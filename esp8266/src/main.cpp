// chilipad-rf: nRF24L01+ promiscuous sniffer for an LT8920-based 2.4 GHz remote.
//
// Technique (Travis Goodspeed's nRF24 "promiscuous mode" trick):
//   * Write SETUP_AW (0x03) = 0x00, which selects an *illegal* 2-byte address
//     width. The nRF24 then matches only 2 address bytes (pipe 0) against the
//     incoming MSB-first bitstream, so any frame whose bits drift across our
//     2-byte "address" gets clocked in as a fixed-length payload.
//   * CRC disabled + auto-ack off + fixed 32-byte payload => every preamble
//     hit yields 32 raw demodulated bytes: preamble + sync word + payload
//     fragments + CRC bytes, all visible in the raw dump.
//   * The LT8920 sends LSB-first; the nRF24 clocking assumes MSB-first, so we
//     print both the RAW bytes and a bit-reversed (REV) view. The REV view is
//     the likely-useful one for recovering the sync word.
//
// Target signal: 2450 MHz (nRF24 channel 50), GFSK 1 Mbps, air frame =
// 3-byte preamble (0xAA/0x55 alternating) | 64-bit sync word (LSB-first on
// air) | 4-bit trailer | payload (first byte = length) | CRC16. Scrambler off.
//
// Serial UI at 115200: single-letter commands, optional number, newline.
// 'h' prints the help menu (also shown at boot).

#include <Arduino.h>
#include <SPI.h>
#include <RF24.h>
#include <ESP8266WiFi.h>

// ---------------------------------------------------------------------------
// Hardware / radio configuration
// ---------------------------------------------------------------------------

static const uint8_t PIN_CE  = D8;  // GPIO15 (board pull-down keeps radio idle at boot)
static const uint8_t PIN_CSN = D2;  // GPIO4  (D5/D6/D7 are the SPI bus)

static const uint8_t REG_SETUP_AW   = 0x03;  // address width
static const uint8_t REG_RX_ADDR_P0 = 0x0A;  // pipe 0 RX address
static const uint8_t REG_TX_ADDR    = 0x10;  // TX address
static const uint8_t REG_RF_SETUP   = 0x06;  // RF setup (chip sanity check)
static const uint8_t REG_CONFIG     = 0x00;  // config (chip sanity check)

static const uint8_t FIXED_LEN = 32;         // fixed payload length to capture
static const uint8_t AW_2BYTE  = 0x00;       // SETUP_AW value: "illegal" 2-byte

// Address variants cycled with 'a'. The nRF24 matches these bytes against the
// MSB-first bitstream. 0xAA/0x55 are the preamble patterns themselves; the
// variants with a leading 0x00 sometimes trigger better when noise precedes
// the preamble (leading zero parks the correlator).
// NOTE: each pair is listed in REGISTER write order (nRF24 multi-byte
// registers are written LSByte first). The on-air bit order relative to this
// intent is unverified, so both orientations (00AA/AA00, 0055/5500) exist —
// cycle with 'a' and use whichever clusters real frames in 't'.
static const uint8_t NUM_ADDR_VARIANTS = 6;
static const uint8_t ADDR_VARIANTS[NUM_ADDR_VARIANTS][2] = {
    {0xAA, 0xAA},  // 0: "AAAA"
    {0x55, 0x55},  // 1: "5555"
    {0x00, 0xAA},  // 2: "00AA"
    {0x00, 0x55},  // 3: "0055"
    {0xAA, 0x00},  // 4: "AA00"
    {0x55, 0x00},  // 5: "5500"
};
static const char *ADDR_VARIANT_NAMES[NUM_ADDR_VARIANTS] = {
    "AAAA", "5555", "00AA", "0055", "AA00", "5500",
};

// ---------------------------------------------------------------------------
// RF24 subclass exposing the protected register primitives.
//
// RF24::setAddressWidth() rejects anything outside 3..5, but the raw register
// write is all we need. Subclassing keeps the trick in one place instead of
// scattering #define-private hacks through the code.
// ---------------------------------------------------------------------------
class RawRF24 : public RF24 {
public:
  RawRF24(uint8_t cePin, uint8_t csPin) : RF24(cePin, csPin) {}

  // RF24 1.4.x keeps write_register() private (read_register is protected),
  // so register writes go through our own SPI transactions. These are only
  // invoked between RF24 calls from our config code, so they can't collide
  // with RF24's own chip-select handling.
  void rawWriteReg(uint8_t reg, const uint8_t *buf, uint8_t len) {
    SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
    digitalWrite(PIN_CSN, LOW);
    SPI.transfer(0x20 | reg);  // W_REGISTER
    for (uint8_t i = 0; i < len; i++) SPI.transfer(buf[i]);
    digitalWrite(PIN_CSN, HIGH);
    SPI.endTransaction();
  }
  void rawWriteReg8(uint8_t reg, uint8_t value) {
    SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
    digitalWrite(PIN_CSN, LOW);
    SPI.transfer(0x20 | reg);  // W_REGISTER
    SPI.transfer(value);
    digitalWrite(PIN_CSN, HIGH);
    SPI.endTransaction();
  }
  uint8_t rawReadReg(uint8_t reg) { return read_register(reg); }
  void rawReadRegBuf(uint8_t reg, uint8_t *buf, uint8_t len) {
    read_register(reg, buf, len);
  }
};

static RawRF24 radio(PIN_CE, PIN_CSN);

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------

static uint8_t  g_channel       = 50;    // 2450 MHz
static uint8_t  g_addrVariant   = 0;     // index into ADDR_VARIANTS
static uint8_t  g_lastAWReadback = 0xFF; // last verified SETUP_AW value
static bool     g_capturing     = false;
static bool     g_haveLastRaw   = false;
static uint8_t  g_lastRaw[FIXED_LEN];

// Custom RX trigger address ('w<hex>'): bytes stored AS GIVEN (the on-air
// order as seen in the RAW view). 'o' flips how they are written to the
// nRF24 address registers, since the on-air byte order vs the register's
// LSByte-first write order is unverified.
static bool     g_customActive   = false;
static uint8_t  g_customBytes[5];
static uint8_t  g_customLen      = 0;     // 3..5 when active
static bool     g_customReversed = false;

// Optional print filter ('f<hex>'): only print frames whose RAW bytes contain
// this byte substring (up to 8 bytes). g_filterLen == 0 means "no filter".
static uint8_t  g_filter[8];
static uint8_t  g_filterLen = 0;

// 't' table: 8-byte prefixes of the REV (bit-reversed) view, fixed 64
// entries, least-count eviction. No heap growth.
static const uint8_t PREFIX_TABLE_SIZE = 64;
struct PrefixEntry {
  uint8_t   bytes[8];
  uint16_t  count;
};
static PrefixEntry g_prefixTable[PREFIX_TABLE_SIZE];  // count==0 => empty

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Reverse the bits of one byte (LT8920 sends LSB-first; nRF24 assumes MSB).
static uint8_t bitReverse8(uint8_t b) {
  b = (b & 0xF0) >> 4 | (b & 0x0F) << 4;
  b = (b & 0xCC) >> 2 | (b & 0x33) << 2;
  b = (b & 0xAA) >> 1 | (b & 0x55) << 1;
  return b;
}

static void hex2(const uint8_t b, char *out) {
  static const char HEXD[] = "0123456789ABCDEF";
  out[0] = HEXD[b >> 4];
  out[1] = HEXD[b & 0x0F];
}

// Render len bytes as uppercase hex (out must hold len*2+1 chars).
static void hexStr(const uint8_t *b, uint8_t len, char *out) {
  for (uint8_t i = 0; i < len; i++) hex2(b[i], out + i * 2);
  out[len * 2] = '\0';
}

// Compute the bytes to program into the nRF24 address registers and the
// matching SETUP_AW value, for the active trigger-address mode:
//   * 2-byte preamble variants ('a'): SETUP_AW=0x00 (the promiscuous trick)
//   * custom trigger ('w<hex>'): SETUP_AW = 3->0x01, 4->0x02, 5->0x03
// The custom bytes are stored as given (on-air RAW order); with 'o' active
// they are written reversed, because the mapping between on-air bit order
// and the register's LSByte-first write order is unverified.
// reg[] is always 5 bytes long (register is physically 5 bytes); only the
// first len bytes are matched and get written to the low register bytes.
static void buildTriggerReg(uint8_t reg[5], uint8_t &len, uint8_t &awVal) {
  if (g_customActive && g_customLen >= 3 && g_customLen <= 5) {
    len = g_customLen;
    for (uint8_t i = 0; i < len; i++) {
      reg[i] = g_customReversed ? g_customBytes[len - 1 - i] : g_customBytes[i];
    }
    awVal = (uint8_t)(len - 2);  // 3->0x01, 4->0x02, 5->0x03
  } else {
    len = 2;
    reg[0] = ADDR_VARIANTS[g_addrVariant][0];
    reg[1] = ADDR_VARIANTS[g_addrVariant][1];
    awVal = AW_2BYTE;
  }
  for (uint8_t i = len; i < 5; i++) reg[i] = 0;  // zero-pad the 5-byte buffer
}

// Program SETUP_AW and the pipe-0 RX address with the active trigger bytes.
// Call this AFTER every RF24 call that might touch address width or pipe 0
// (openReadingPipe, openWritingPipe, begin, ...). RF24 caches address widths
// internally but never reads SETUP_AW back on the data path, so re-writing
// the register here is authoritative.
static void forcePromiscuousAddress() {
  uint8_t reg[5];
  uint8_t len;
  uint8_t awVal;
  buildTriggerReg(reg, len, awVal);

  // Apply the address width (0x00 = 2-byte trick, 0x01..0x03 = 3..5 bytes).
  radio.rawWriteReg8(REG_SETUP_AW, awVal);

  // Re-program pipe 0 (RX side) with our trigger bytes, matching AW.
  radio.rawWriteReg(REG_RX_ADDR_P0, reg, len);

  // Verify by reading SETUP_AW back; log anything unexpected.
  g_lastAWReadback = radio.rawReadReg(REG_SETUP_AW) & 0x03;
  if (g_lastAWReadback != awVal) {
    Serial.printf("[warn] SETUP_AW readback = 0x%02X (expected 0x%02X)\r\n",
                  g_lastAWReadback, awVal);
  }
}

// Program the TX address registers for replay (TX_ADDR + pipe0 mirror).
static void forcePromiscuousTxAddress() {
  uint8_t reg[5];
  uint8_t len;
  uint8_t awVal;
  buildTriggerReg(reg, len, awVal);
  radio.rawWriteReg8(REG_SETUP_AW, awVal);
  radio.rawWriteReg(REG_TX_ADDR, reg, len);
  radio.rawWriteReg(REG_RX_ADDR_P0, reg, len);  // RF24 mirrors pipe0 for ACKs
}

// Apply the full promiscuous RX configuration. Re-applies SETUP_AW=0 after
// every RF24 call that could rewrite it, and prints the readback.
static void applyRxConfig() {
  radio.stopListening();
  radio.setChannel(g_channel);
  radio.setDataRate(RF24_1MBPS);
  radio.setPALevel(RF24_PA_MAX);
  radio.setAutoAck(false);              // all pipes
  radio.disableDynamicPayloads();
  radio.setPayloadSize(FIXED_LEN);
  radio.setCRCLength(RF24_CRC_DISABLED);
  radio.closeReadingPipe(1);            // only pipe 0 stays open

  // RF24 (>=1.6) caches pipe-0 addresses and re-writes RX_ADDR_P0 itself:
  //   * startListening(): write_register(RX_ADDR_P0, pipe0_reading_address)
  //   * stopListening():  write_register(RX_ADDR_P0, pipe0_writing_address)
  // If the caches hold anything but our trigger bytes, every RX/TX switch
  // overwrites the trigger address with junk. So open pipe 0 with a 5-byte
  // buffer whose first bytes are our trigger: the register is physically 5
  // bytes and only the SETUP_AW-selected low bytes are matched, so the
  // trailing zeros are harmless — and both RF24 caches now agree with us.
  uint8_t reg[5];
  uint8_t len;
  uint8_t awVal;
  buildTriggerReg(reg, len, awVal);
  radio.openWritingPipe(reg);    // cache pipe0_writing_address + TX_ADDR
  radio.openReadingPipe(0, reg); // cache pipe0_reading_address

  // Now apply the actual address width (promiscuous trick or custom AW) and
  // re-program the low register bytes ourselves while CE is low (standby:
  // W_REGISTER is legal here, unlike after startListening()).
  forcePromiscuousAddress();
  radio.flush_rx();

  // Debug readback (read-only, legal in any state): show the trigger address
  // actually on the chip next to the SETUP_AW readback.
  uint8_t p0rb[5];
  radio.rawReadRegBuf(REG_RX_ADDR_P0, p0rb, len);
  char p0hx[11];
  hexStr(p0rb, len, p0hx);
  Serial.printf("[cfg] ch=%u addr=%s SETUP_AW=0x%02X "
                "RX_ADDR_P0=%s len=%u CRC=off ack=off\r\n",
                g_channel,
                g_customActive ? "custom" : ADDR_VARIANT_NAMES[g_addrVariant],
                g_lastAWReadback, p0hx, FIXED_LEN);
}

// Route ALL radio.startListening() calls through here. startListening()
// restores RX_ADDR_P0 from RF24's pipe0_reading_address cache (which now
// holds our trigger bytes); afterwards we can only READ the chip, so print
// the on-chip trigger address + SETUP_AW readback once per config change.
// The quiet variant (verbose=false) skips all reads/prints and is used by the
// channel-scan loop, which calls startListening ~250 times per sweep.
static void startRx(bool verbose = true) {
  radio.startListening();
  if (!verbose) return;
  uint8_t aw = radio.rawReadReg(REG_SETUP_AW) & 0x03;
  g_lastAWReadback = aw;
  uint8_t n = (aw == 0) ? 2 : (uint8_t)(aw + 2);  // AW value -> byte count
  if (n > 5) n = 5;
  uint8_t p0rb[5];
  radio.rawReadRegBuf(REG_RX_ADDR_P0, p0rb, n);
  char hx[11];
  hexStr(p0rb, n, hx);
  Serial.printf("[rx] SETUP_AW=0x%02X RX_ADDR_P0=%s (on chip)\r\n", aw, hx);
}

static void printConfig() {
  Serial.printf("channel       : %u (%.3f GHz)\r\n", g_channel,
                2.400 + g_channel * 0.001);
  if (g_customActive) {
    char hx[11];
    hexStr(g_customBytes, g_customLen, hx);
    Serial.printf("trigger      : custom %s (%u bytes, register order: %s)\r\n",
                  hx, g_customLen,
                  g_customReversed ? "reversed" : "as-given");
  } else {
    Serial.printf("trigger      : variant %s (0x%02X 0x%02X)\r\n",
                  ADDR_VARIANT_NAMES[g_addrVariant],
                  ADDR_VARIANTS[g_addrVariant][0],
                  ADDR_VARIANTS[g_addrVariant][1]);
  }
  if (g_lastAWReadback == 0xFF) {
    Serial.println(F("SETUP_AW      : (radio not initialised)"));
    Serial.println(F("chip check    : (radio not initialised)"));
  } else {
    Serial.printf("SETUP_AW      : 0x%02X (0x00 = 2-byte trick, 0x01..3 = 3..5 B)\r\n",
                  g_lastAWReadback);
    // RF24::isChipConnected() only checks SETUP_AW in 1..3, which the AW=0
    // promiscuous trick always fails. Do our own SPI sanity check instead:
    // a dead/floating bus reads 0xFF, a held reset reads 0x00.
    uint8_t rfSetup = radio.rawReadReg(REG_RF_SETUP);
    uint8_t cfgReg = radio.rawReadReg(REG_CONFIG);
    bool alive = !(rfSetup == 0x00 || rfSetup == 0xFF ||
                   cfgReg == 0x00 || cfgReg == 0xFF);
    Serial.printf("chip check    : %s (RF_SETUP=0x%02X CONFIG=0x%02X)\r\n",
                  alive ? "ok" : "NO SPI RESPONSE", rfSetup, cfgReg);
  }
  Serial.printf("capturing     : %s\r\n", g_capturing ? "yes" : "no");
  Serial.printf("filter        : %s\r\n",
                g_filterLen == 0 ? "(none)"
                                 : "(active, see last 'f' command)");
}

static void printHelp() {
  Serial.println(F(
      "\r\n=== chilipad-rf promiscuous sniffer ===\r\n"
      "h      help + current config\r\n"
      "s<n>   channel scan, n seconds (default 2, max 30; press remote keys)\r\n"
      "c<n>   set channel 0..125 (default 50 = 2450 MHz)\r\n"
      "a      cycle address variant: AAAA, 5555, 00AA, 0055, AA00, 5500\r\n"
      "w<hex> custom RX trigger, 3-5 bytes in on-air RAW order\r\n"
      "       (e.g. w0B5C8E03); 'w' alone = back to the 2-byte variants\r\n"
      "o      flip custom trigger byte order (as-given <-> reversed)\r\n"
      "p      start/stop promiscuous capture (RAW + bit-reversed REV lines)\r\n"
      "t      top 10 most frequent 8-byte prefixes of REV frames\r\n"
      "z      clear the prefix table\r\n"
      "f<hex> print-filter: RAW frames containing this hex substring (<=8 B);\r\n"
      "       'f' alone clears the filter\r\n"
      "x<n>   replay last captured RAW frame n times (default 10)\r\n"));
  printConfig();
}

// ---------------------------------------------------------------------------
// Prefix frequency table ('t' / 'z')
// ---------------------------------------------------------------------------

static void prefixTableClear() { memset(g_prefixTable, 0, sizeof(g_prefixTable)); }

static void prefixTableAdd(const uint8_t *raw) {
  // The table and 't' output document REV prefixes: bit-reverse the first 8
  // bytes before storing/counting.
  uint8_t rev[8];
  for (uint8_t i = 0; i < 8; i++) rev[i] = bitReverse8(raw[i]);

  // Match existing entry.
  for (uint8_t i = 0; i < PREFIX_TABLE_SIZE; i++) {
    if (g_prefixTable[i].count != 0 &&
        memcmp(g_prefixTable[i].bytes, rev, 8) == 0) {
      if (g_prefixTable[i].count != 0xFFFF) g_prefixTable[i].count++;
      return;
    }
  }
  // Free slot?
  for (uint8_t i = 0; i < PREFIX_TABLE_SIZE; i++) {
    if (g_prefixTable[i].count == 0) {
      memcpy(g_prefixTable[i].bytes, rev, 8);
      g_prefixTable[i].count = 1;
      return;
    }
  }
  // Full: evict the entry with the smallest count.
  uint8_t minIdx = 0;
  for (uint8_t i = 1; i < PREFIX_TABLE_SIZE; i++) {
    if (g_prefixTable[i].count < g_prefixTable[minIdx].count) minIdx = i;
  }
  memcpy(g_prefixTable[minIdx].bytes, rev, 8);
  g_prefixTable[minIdx].count = 1;
}

static void prefixTablePrint() {
  bool printed = false;
  Serial.println(F("top 10 REV 8-byte prefixes:"));
  for (uint8_t rank = 0; rank < 10; rank++) {
    uint8_t best = 0xFF;
    uint16_t bestCount = 0;
    for (uint8_t i = 0; i < PREFIX_TABLE_SIZE; i++) {
      if (g_prefixTable[i].count != 0 && g_prefixTable[i].count > bestCount) {
        bestCount = g_prefixTable[i].count;
        best = i;
      }
    }
    if (best == 0xFF) break;
    char line[8 * 2 + 1];
    for (uint8_t b = 0; b < 8; b++) hex2(g_prefixTable[best].bytes[b], line + b * 2);
    line[16] = '\0';
    Serial.printf("  %2u. %s  x%u\r\n", rank + 1, line, bestCount);
    g_prefixTable[best].count = 0;  // suppress re-selection next rank
    printed = true;
  }
  if (!printed) Serial.println(F("  (table empty; run 'p' while pressing buttons)"));
}

// ---------------------------------------------------------------------------
// Capture printing
// ---------------------------------------------------------------------------

// Does the RAW frame contain the filter byte substring?
static bool frameMatchesFilter(const uint8_t *raw) {
  if (g_filterLen == 0) return true;
  if (g_filterLen > FIXED_LEN) return false;
  for (uint8_t start = 0; start + g_filterLen <= FIXED_LEN; start++) {
    if (memcmp(raw + start, g_filter, g_filterLen) == 0) return true;
  }
  return false;
}

static void printFrame(const uint8_t *raw, unsigned long ms) {
  // One cheap print per view: assemble the whole line on the stack.
  char line[FIXED_LEN * 2 + 1];
  char rev[FIXED_LEN * 2 + 1];
  for (uint8_t i = 0; i < FIXED_LEN; i++) {
    hex2(raw[i], line + i * 2);
    hex2(bitReverse8(raw[i]), rev + i * 2);
  }
  line[FIXED_LEN * 2] = '\0';
  rev[FIXED_LEN * 2] = '\0';
  Serial.printf("%lu RAW %s | REV %s\r\n", ms, line, rev);
}

// ---------------------------------------------------------------------------
// Channel scan ('s')
// ---------------------------------------------------------------------------

static void channelScan(uint8_t seconds) {
  if (seconds == 0) seconds = 2;   // 's' with no number -> default 2 s
  if (seconds > 30) seconds = 30;  // clamp
  Serial.printf("[scan] %u s carrier-detect sweep, press remote buttons...\r\n",
                seconds);
  static uint16_t hits[126];
  memset(hits, 0, sizeof(hits));

  unsigned long start = millis();
  uint32_t sweeps = 0;
  // Scanner-example pattern per channel. The RPD window is only ~40 us and
  // the PLL needs ~130 us to settle after setChannel, so dwell >= 250 us:
  //   stopListening (CE low) -> setChannel (PLL settles) -> startListening
  //   (CE high, quiet) -> dwell 250 us -> stopListening -> testRPD (RPD
  //   latches the carrier-detect result of the dwell window)
  while (millis() - start < seconds * 1000UL) {
    for (uint8_t ch = 0; ch <= 125; ch++) {
      radio.stopListening();  // CE low
      radio.setChannel(ch);   // PLL settles while CE low
      startRx(false);         // CE high (quiet: no per-channel print)
      delayMicroseconds(250);
      radio.stopListening();  // CE low again; RPD latched
      if (radio.testRPD()) {
        if (hits[ch] != 0xFFFF) hits[ch]++;
      }
    }
    sweeps++;
    yield();  // feed the ESP8266 watchdog between sweeps
  }

  // Top 5 channels for highlighting.
  uint8_t top5[5];
  uint16_t top5hits[5] = {0, 0, 0, 0, 0};
  for (uint8_t ch = 0; ch <= 125; ch++) {
    for (uint8_t k = 0; k < 5; k++) {
      if (hits[ch] > top5hits[k]) {
        for (uint8_t m = 4; m > k; m--) {
          top5[m] = top5[m - 1];
          top5hits[m] = top5hits[m - 1];
        }
        top5[k] = ch;
        top5hits[k] = hits[ch];
        break;
      }
    }
  }
  bool isTop5[126];
  memset(isTop5, 0, sizeof(isTop5));
  for (uint8_t k = 0; k < 5; k++) {
    if (top5hits[k] > 0) isTop5[top5[k]] = true;
  }

  Serial.printf("[scan] %lu sweeps done; channels with activity:\r\n",
                (unsigned long)sweeps);
  for (uint8_t ch = 0; ch <= 125; ch++) {
    if (hits[ch] > 0) {
      Serial.printf("  ch %3u: %4u%s\r\n", ch, hits[ch],
                    isTop5[ch] ? "  <-- top 5" : "");
    }
  }
  Serial.print(F("[scan] top 5:"));
  for (uint8_t k = 0; k < 5; k++) {
    if (top5hits[k] > 0) Serial.printf(" ch%u(%u)", top5[k], top5hits[k]);
  }
  Serial.println();
  // Restore the configured channel / RX state.
  applyRxConfig();
  if (g_capturing) startRx();
}

// ---------------------------------------------------------------------------
// Replay ('x<n>')
// ---------------------------------------------------------------------------

static void replayFrame(uint16_t times) {
  if (!g_haveLastRaw) {
    Serial.println(F("[replay] no captured frame yet; use 'p' first"));
    return;
  }
  if (times == 0) times = 10;
  bool wasCapturing = g_capturing;
  uint8_t reg[5];
  uint8_t len;
  uint8_t awVal;
  buildTriggerReg(reg, len, awVal);
  char addrHex[11];
  hexStr(reg, len, addrHex);
  Serial.printf("[replay] TX ch=%u addr=%s(%s) SETUP_AW=0x%02X %ux: ",
                g_channel,
                g_customActive ? "custom" : ADDR_VARIANT_NAMES[g_addrVariant],
                addrHex, awVal, times);
  radio.stopListening();
  radio.flush_tx();
  // Give RF24's pipe0_writing_address cache our trigger bytes first, so its
  // own TX-side register writes (openWritingPipe writes RX_ADDR_P0/TX_ADDR at
  // cached addr_width) land on our leading bytes. Then apply SETUP_AW and the
  // raw register writes while CE is low (standby).
  radio.openWritingPipe(reg);
  forcePromiscuousTxAddress();  // trigger address, CRC/ack already off
  for (uint16_t i = 0; i < times; i++) {
    radio.write(g_lastRaw, FIXED_LEN, true);  // no-ack write
    delay(5);                                 // ~5 ms between bursts
    yield();
  }
  char line[FIXED_LEN * 2 + 1];
  for (uint8_t i = 0; i < FIXED_LEN; i++) hex2(g_lastRaw[i], line + i * 2);
  line[FIXED_LEN * 2] = '\0';
  Serial.printf("\r\n[replay] sent %ux RAW %s\r\n", times, line);

  // Return to RX if capture was active.
  applyRxConfig();
  if (wasCapturing) startRx();
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
  WiFi.mode(WIFI_OFF);       // not needed; frees RF calibration and power
  WiFi.forceSleepBegin();
  Serial.begin(115200);
  delay(200);
  printHelp();

  // Bring the radio up, retrying with a clear wiring/capacitor hint rather
  // than hanging silently.
  bool up = false;
  while (!up) {
    if (radio.begin() && radio.isChipConnected()) {
      up = true;
    } else {
      Serial.println(F(
          "[err] nRF24L01+ not responding.\r\n"
          "      Check wiring (CE=D8/GPIO15, CSN=D2/GPIO4, SCK=D5, MOSI=D7,\r\n"
          "      MISO=D6) and add a 10 uF capacitor across VCC/GND on the\r\n"
          "      nRF24 module (brown-out resets the radio at PA_MAX).\r\n"
          "      Retrying in 2 s..."));
      delay(2000);
      yield();
    }
  }
  applyRxConfig();
  prefixTableClear();
  Serial.println(F("[ok] radio up. 'h' for help, 's' to scan, 'p' to capture."));
}

// ---------------------------------------------------------------------------
// Serial command handling
// ---------------------------------------------------------------------------

static char    g_cmdBuf[24];
static uint8_t g_cmdLen = 0;

static void handleCommand(const char *cmd) {
  char letter = cmd[0];
  // Optional decimal number after the letter ('c42', 'x10', ...).
  long num = -1;
  if (cmd[1] != '\0') {
    char *end = nullptr;
    num = strtol(cmd + 1, &end, 10);
    if (end == cmd + 1 || *end != '\0') num = -1;  // not a clean number
  }

  switch (letter) {
    case 'h':
      printHelp();
      break;

    case 's':
      channelScan(num >= 0 ? (uint8_t)num : 0);  // 0 -> default 2 s
      break;

    case 'c':
      if (num >= 0 && num <= 125) {
        g_channel = (uint8_t)num;
        applyRxConfig();
        if (g_capturing) startRx();
        Serial.printf("[cfg] channel -> %u (%.3f GHz)\r\n", g_channel,
                      2.400 + g_channel * 0.001);
      } else {
        Serial.printf("[cfg] channel stays %u (usage: c<n>, n=0..125)\r\n",
                      g_channel);
      }
      break;

    case 'a':
      if (g_customActive) {
        // A custom trigger is active: switch back to the 2-byte variant list
        // at the currently selected variant.
        g_customActive = false;
        applyRxConfig();
        if (g_capturing) startRx();
        Serial.printf("[addr] custom trigger off, back to variant %s\r\n",
                      ADDR_VARIANT_NAMES[g_addrVariant]);
      } else {
        g_addrVariant = (g_addrVariant + 1) % NUM_ADDR_VARIANTS;
        applyRxConfig();
        if (g_capturing) startRx();
        Serial.printf("[cfg] address variant -> %s\r\n",
                      ADDR_VARIANT_NAMES[g_addrVariant]);
      }
      break;

    case 'w': {
      if (cmd[1] == '\0') {
        // 'w' alone: return to the 2-byte preamble-variant behavior.
        if (g_customActive) {
          g_customActive = false;
          applyRxConfig();
          if (g_capturing) startRx();
          Serial.printf("[addr] custom trigger off, back to variant %s\r\n",
                        ADDR_VARIANT_NAMES[g_addrVariant]);
        } else {
          Serial.printf("[addr] already in 2-byte variant mode (%s); "
                        "w<hex> sets a custom 3-5 byte trigger\r\n",
                        ADDR_VARIANT_NAMES[g_addrVariant]);
        }
        break;
      }
      // Parse contiguous hex digits after 'w' (3-5 full bytes expected).
      uint8_t bytes[5];
      uint8_t nib = 0;
      uint8_t cur = 0;
      bool bad = false;
      const char *p = cmd + 1;
      while (*p) {
        char c = *p++;
        int8_t v;
        if (c >= '0' && c <= '9') v = c - '0';
        else if (c >= 'a' && c <= 'f') v = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') v = c - 'A' + 10;
        else { bad = true; break; }  // only hex digits accepted
        if (nib >= 10) { bad = true; break; }  // more than 5 bytes
        if (nib & 1) bytes[nib / 2] = (uint8_t)((cur << 4) | v);
        else cur = (uint8_t)v;
        nib++;
      }
      if (bad || (nib & 1) != 0 || nib / 2 < 3 || nib / 2 > 5) {
        Serial.println(F("[addr] usage: w<hex> = 3-5 bytes, e.g. w0B5C8E03 "
                         "('w' alone = 2-byte variants)"));
        break;
      }
      memcpy(g_customBytes, bytes, sizeof(bytes));
      g_customLen = nib / 2;
      g_customActive = true;
      g_customReversed = false;  // a new address starts as-given; 'o' flips
      applyRxConfig();
      if (g_capturing) startRx();
      char hx[11];
      hexStr(g_customBytes, g_customLen, hx);
      Serial.printf("[addr] custom trigger %s (%u bytes, register order: "
                    "as-given; 'o' flips)\r\n", hx, g_customLen);
      break;
    }

    case 'o':
      if (!g_customActive) {
        Serial.println(F("[addr] 'o' only applies to a custom w<hex> trigger "
                         "(the 2-byte variants already cover both orders)"));
        break;
      }
      g_customReversed = !g_customReversed;
      applyRxConfig();
      if (g_capturing) startRx();
      Serial.printf("[addr] custom trigger register order -> %s\r\n",
                    g_customReversed ? "reversed" : "as-given");
      break;

    case 'p':
      g_capturing = !g_capturing;
      if (g_capturing) {
        startRx();
        Serial.println(F("[cap] capture ON"));
      } else {
        radio.stopListening();
        Serial.println(F("[cap] capture OFF"));
      }
      break;

    case 't':
      prefixTablePrint();
      break;

    case 'z':
      prefixTableClear();
      Serial.println(F("[tbl] prefix table cleared"));
      break;

    case 'f': {
      // Parse hex digits after 'f' (up to 16 hex chars = 8 bytes).
      const char *p = cmd + 1;
      g_filterLen = 0;
      bool nibble = false;
      uint8_t cur = 0;
      while (*p && g_filterLen < 8) {
        char c = *p++;
        int8_t v;
        if (c >= '0' && c <= '9') v = c - '0';
        else if (c >= 'a' && c <= 'f') v = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') v = c - 'A' + 10;
        else continue;  // skip separators/spaces
        if (nibble) {
          g_filter[g_filterLen++] = (cur << 4) | (uint8_t)v;
          nibble = false;
        } else {
          cur = (uint8_t)v;
          nibble = true;
        }
      }
      if (nibble) { g_filter[g_filterLen++] = cur; }  // trailing nibble -> low
      if (g_filterLen == 0) {
        Serial.println(F("[flt] filter cleared"));
      } else {
        char line[17];
        for (uint8_t i = 0; i < g_filterLen; i++) hex2(g_filter[i], line + i * 2);
        line[g_filterLen * 2] = '\0';
        Serial.printf("[flt] filter ON: %s (%u bytes of RAW must contain it)\r\n",
                      line, g_filterLen);
      }
      break;
    }

    case 'x':
      replayFrame(num >= 0 ? (uint16_t)num : 10);
      break;

    case '\0':
      break;  // bare newline: ignore

    default:
      Serial.printf("[?] unknown command '%c' ('h' for help)\r\n", letter);
      break;
  }
}

static void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      g_cmdBuf[g_cmdLen] = '\0';
      if (g_cmdLen > 0) handleCommand(g_cmdBuf);
      g_cmdLen = 0;
    } else if (g_cmdLen < sizeof(g_cmdBuf) - 1) {
      g_cmdBuf[g_cmdLen++] = c;
    }
    yield();
  }
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

void loop() {
  pollSerial();

  if (g_capturing) {
    // Drain everything the radio has queued. Printing is one printf per view;
    // if a burst floods us we keep draining without blocking on the UART.
    uint8_t drained = 0;
    while (radio.available()) {
      uint8_t raw[FIXED_LEN];
      radio.read(raw, FIXED_LEN);
      memcpy(g_lastRaw, raw, FIXED_LEN);
      g_haveLastRaw = true;

      prefixTableAdd(raw);  // counts REV prefixes of everything captured;
                            // the 'f' filter only gates printing

      if (frameMatchesFilter(raw)) printFrame(raw, millis());
      if (++drained >= 16) { yield(); drained = 0; }
    }
    delay(1);  // yield to the ESP8266 scheduler / watchdog
  } else {
    delay(5);
  }
}
