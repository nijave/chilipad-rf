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

static const uint8_t PIN_CE  = D2;  // GPIO4
static const uint8_t PIN_CSN = D8;  // GPIO15 (D5/D6/D7 are the SPI bus)

static const uint8_t REG_SETUP_AW   = 0x03;  // address width
static const uint8_t REG_RX_ADDR_P0 = 0x0A;  // pipe 0 RX address
static const uint8_t REG_TX_ADDR    = 0x10;  // TX address

static const uint8_t FIXED_LEN = 32;         // fixed payload length to capture
static const uint8_t AW_2BYTE  = 0x00;       // SETUP_AW value: "illegal" 2-byte

// Address variants cycled with 'a'. The nRF24 matches these bytes against the
// MSB-first bitstream. 0xAA/0x55 are the preamble patterns themselves; the
// 0x00,AA / 0x00,55 variants sometimes trigger better when noise precedes the
// preamble (leading zero parks the correlator).
static const uint8_t NUM_ADDR_VARIANTS = 4;
static const uint8_t ADDR_VARIANTS[NUM_ADDR_VARIANTS][2] = {
    {0xAA, 0xAA},  // 0: "AAAA"
    {0x55, 0x55},  // 1: "5555"
    {0x00, 0xAA},  // 2: "00AA"
    {0x00, 0x55},  // 3: "0055"
};
static const char *ADDR_VARIANT_NAMES[NUM_ADDR_VARIANTS] = {
    "AAAA", "5555", "00AA", "0055",
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

// Optional print filter ('f<hex>'): only print frames whose RAW bytes contain
// this byte substring (up to 8 bytes). g_filterLen == 0 means "no filter".
static uint8_t  g_filter[8];
static uint8_t  g_filterLen = 0;

// Frequency table for 't': 8-byte prefixes of REV frames, fixed 64 entries,
// least-count eviction. No heap growth.
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

// Set SETUP_AW=0 (2-byte illegal address) and program the pipe-0 address with
// exactly 2 bytes. Call this AFTER every RF24 call that might touch address
// width or pipe 0 (openReadingPipe, openWritingPipe, begin, ...). RF24 caches
// address widths internally but never reads SETUP_AW back on the data path,
// so re-writing the register here is authoritative.
static void forcePromiscuousAddress() {
  const uint8_t *addr = ADDR_VARIANTS[g_addrVariant];

  // The trick: SETUP_AW = 0x00 => 2-byte address width.
  radio.rawWriteReg8(REG_SETUP_AW, AW_2BYTE);

  // Re-program pipe 0 (RX side) with only our 2 bytes, matching AW=2.
  uint8_t pipe0[2] = { addr[0], addr[1] };
  radio.rawWriteReg(REG_RX_ADDR_P0, pipe0, 2);

  // Verify by reading SETUP_AW back; log anything unexpected.
  g_lastAWReadback = radio.rawReadReg(REG_SETUP_AW) & 0x03;
  if (g_lastAWReadback != AW_2BYTE) {
    Serial.printf("[warn] SETUP_AW readback = 0x%02X (expected 0x00)\r\n",
                  g_lastAWReadback);
  }
}

// Program the TX address registers for replay (TX_ADDR + pipe0 mirror).
static void forcePromiscuousTxAddress() {
  const uint8_t *addr = ADDR_VARIANTS[g_addrVariant];
  uint8_t two[2] = { addr[0], addr[1] };
  radio.rawWriteReg8(REG_SETUP_AW, AW_2BYTE);
  radio.rawWriteReg(REG_TX_ADDR, two, 2);
  radio.rawWriteReg(REG_RX_ADDR_P0, two, 2);  // RF24 mirrors pipe0 for ACKs
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
  radio.openReadingPipe(0, 0ULL);       // open pipe 0; address set raw below
  forcePromiscuousAddress();            // re-apply the trick afterwards
  radio.flush_rx();
  Serial.printf("[cfg] ch=%u variant=%s SETUP_AW=0x%02X len=%u CRC=off "
                "ack=off\r\n",
                g_channel, ADDR_VARIANT_NAMES[g_addrVariant], g_lastAWReadback,
                FIXED_LEN);
}

static void printConfig() {
  Serial.printf("channel      : %u (%.3f GHz)\r\n", g_channel,
                2.400 + g_channel * 0.001);
  Serial.printf("addr variant : %s (0x%02X 0x%02X)\r\n",
                ADDR_VARIANT_NAMES[g_addrVariant], ADDR_VARIANTS[g_addrVariant][0],
                ADDR_VARIANTS[g_addrVariant][1]);
  Serial.printf("SETUP_AW     : 0x%02X (0x00 = 2-byte illegal width)\r\n",
                g_lastAWReadback);
  Serial.printf("chip connected: %s\r\n",
                radio.isChipConnected() ? "yes" : "NO");
  Serial.printf("capturing    : %s\r\n", g_capturing ? "yes" : "no");
  Serial.printf("filter       : %s\r\n",
                g_filterLen == 0 ? "(none)"
                                 : "(active, see last 'f' command)");
}

static void printHelp() {
  Serial.println(F(
      "\r\n=== chilipad-rf promiscuous sniffer ===\r\n"
      "h      help + current config\r\n"
      "s      channel scan (2 s, testRPD carrier detect; press remote keys)\r\n"
      "c<n>   set channel 0..125 (default 50 = 2450 MHz)\r\n"
      "a      cycle address variant: AAAA, 5555, 00AA, 0055\r\n"
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

static void prefixTableAdd(const uint8_t *rev) {
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

static void channelScan() {
  Serial.println(F("[scan] 2 s carrier-detect sweep, press remote buttons..."));
  const unsigned long DURATION_MS = 2000;
  const unsigned long PER_CHANNEL_US = 150;  // RPD settles in ~100 us
  static uint16_t hits[126];
  memset(hits, 0, sizeof(hits));

  radio.startListening();
  unsigned long start = millis();
  uint32_t sweeps = 0;
  while (millis() - start < DURATION_MS) {
    for (uint8_t ch = 0; ch <= 125; ch++) {
      radio.setChannel(ch);
      delayMicroseconds(PER_CHANNEL_US);
      if (radio.testRPD()) {
        if (hits[ch] != 0xFFFF) hits[ch]++;
      }
    }
    sweeps++;
    yield();  // feed the ESP8266 watchdog between sweeps
  }
  radio.stopListening();

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
  if (g_capturing) radio.startListening();
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
  Serial.printf("[replay] TX ch=%u addr=%s %ux: ", g_channel,
                ADDR_VARIANT_NAMES[g_addrVariant], times);
  radio.stopListening();
  radio.flush_tx();
  forcePromiscuousTxAddress();  // 2-byte TX address, AW=0, CRC/ack already off
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
  if (wasCapturing) radio.startListening();
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
          "      Check wiring (CE=D2/GPIO4, CSN=D8/GPIO15, SCK=D5, MOSI=D7,\r\n"
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
      channelScan();
      break;

    case 'c':
      if (num >= 0 && num <= 125) {
        g_channel = (uint8_t)num;
        applyRxConfig();
        if (g_capturing) radio.startListening();
        Serial.printf("[cfg] channel -> %u (%.3f GHz)\r\n", g_channel,
                      2.400 + g_channel * 0.001);
      } else {
        Serial.printf("[cfg] channel stays %u (usage: c<n>, n=0..125)\r\n",
                      g_channel);
      }
      break;

    case 'a':
      g_addrVariant = (g_addrVariant + 1) % NUM_ADDR_VARIANTS;
      applyRxConfig();
      if (g_capturing) radio.startListening();
      Serial.printf("[cfg] address variant -> %s\r\n",
                    ADDR_VARIANTS[g_addrVariant]);
      break;

    case 'p':
      g_capturing = !g_capturing;
      if (g_capturing) {
        radio.startListening();
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

      prefixTableAdd(raw);  // table counts everything; filter only gates prints

      if (frameMatchesFilter(raw)) printFrame(raw, millis());
      if (++drained >= 16) { yield(); drained = 0; }
    }
    delay(1);  // yield to the ESP8266 scheduler / watchdog
  } else {
    delay(5);
  }
}
