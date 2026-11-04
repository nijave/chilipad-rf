/*
 * chilipad-sniffer: raw 2.4 GHz sniffer for the LT8920 based chilipad remote,
 * running on a Nordic nRF52840 Dongle (PCA10059) under Zephyr.
 *
 * This is the nRF52 sibling of the ESP8266 + nRF24L01+ promiscuous sniffer in
 * ../../src/main.cpp.  Instead of Travis Goodspeed's "illegal 2-byte address"
 * trick it uses the nRF52840 RADIO peripheral directly: the LT8920's sync word
 * (or any 3..5 byte prefix of the frame after the preamble) is loaded as a
 * logical RX address, so the radio only ends a reception on a real frame.  The
 * payload that follows the address is dumped in the same RAW / REV form the
 * nRF24 firmware produces, so the laptop GUI can parse either receiver.
 *
 * No Bluetooth stack, no ESB library, no SoftDevice: direct NRF_RADIO,
 * NRF_TIMER1 and NRF_PPI register access (via the nrfx HAL inline accessors).
 *
 * ---------------------------------------------------------------------------
 * Address mapping (why the default is "as given" + "bit reversed")
 * ---------------------------------------------------------------------------
 * nRF52840 Product Specification, RADIO chapter:
 *   "The byte ordering on air is always least significant byte first for the
 *    ADDRESS and PAYLOAD fields ... The ADDRESS fields are always transmitted
 *    and received least significant bit first."
 * Logical address 0 is BASE0 followed by PREFIX0.AP0, where AP0 is the *most*
 * significant byte of the address and therefore the last one on air.  The base
 * is truncated "from the least significant byte if PCNF1.BALEN is less than
 * 4".  So for a 4 byte address A B C D (on-air order):
 *
 *   BASE0[7:0]   = unused (truncated away because BALEN = 3)
 *   BASE0[15:8]  = A      (first byte on air)
 *   BASE0[23:16] = B
 *   BASE0[31:24] = C
 *   PREFIX0.AP0  = D      (last byte on air), BALEN = 3
 *
 * The byte order therefore needs no fiddling: first on-air byte -> lowest
 * BASE0 byte.  The *bit* order does: the nRF24/LT8920 RAW view this tool
 * mirrors presents each address byte MSB first (that is why the ESP firmware
 * has to bit-reverse every byte for its REV view), while the nRF52 compares
 * address bits LSBit first.  Every address byte is therefore bit-reversed
 * before it is written to BASE0/PREFIX0 - exactly the bit swap Nordic's own
 * ESB driver performs in addr_conv().  Default: order as given, bit reversal
 * ON.  'o' and 'r' flip the two halves at runtime so all four combinations
 * can be tried against a live signal.
 *
 * ---------------------------------------------------------------------------
 * Receive path
 * ---------------------------------------------------------------------------
 * Two payload buffers are alternated.  PACKETPTR is latched when START runs,
 * so the ADDRESS interrupt points PACKETPTR at the *other* buffer while the
 * current packet is still being received; the END_START short then starts the
 * next reception with the new pointer already in place.  END pushes the
 * finished buffer (timestamp captured at its ADDRESS event, RSSI, channel)
 * into a fixed ring and the main thread does all of the printing.
 *
 * A packet that matches no address never produces END - it aborts and raises
 * DISABLED, and the ISR simply starts the receiver again.
 */

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/ring_buffer.h>

#include <nrfx.h>
#include <hal/nrf_clock.h>
#include <hal/nrf_ppi.h>
#include <hal/nrf_radio.h>
#include <hal/nrf_timer.h>

#include <zephyr/drivers/clock_control/nrf_clock_control.h>
#include <zephyr/sys/onoff.h>

/* ------------------------------------------------------------------------- */
/* Tunables and fixed sizes                                                  */
/* ------------------------------------------------------------------------- */

#define PAYLOAD_MAX       255           /* PCNF1.MAXLEN/STATLEN are 8 bit */
#define RX_BUFS           2             /* double buffering, see top comment */
#define PKT_RING_LEN      16            /* ISR -> main thread packet ring */
#define TX_RING_BYTES     8192          /* non-blocking console output ring */
#define TX_CHUNK_BYTES    64
#define LINE_MAX          1400          /* one printed line, no allocation */

#define DEFAULT_CHANNEL   50            /* 2450 MHz */
#define DEFAULT_LEN       64
#define DEFAULT_ADDR_LEN  4
#define CHANNEL_MAX       100

/* TIMER1 is free on this board (the kernel clock uses RTC1, no nrfx timer
 * driver is enabled and there is no radio/802.15.4 stack).  PRESCALER 4 turns
 * the 16 MHz peripheral clock into the 1 MHz timestamp tick. */
#define CAPTURE_TIMER     NRF_TIMER1
#define CAPTURE_PRESCALE  4
#define CAPTURE_PPI_CH    NRF_PPI_CHANNEL0

/* RADIO events that drive the receive path. */
#define RADIO_INT_MASK    (NRF_RADIO_INT_ADDRESS_MASK | \
			   NRF_RADIO_INT_END_MASK | \
			   NRF_RADIO_INT_DISABLED_MASK)

/* ------------------------------------------------------------------------- */
/* State                                                                     */
/* ------------------------------------------------------------------------- */

/* Radio configuration (touched by the main thread only, with the RADIO IRQ
 * masked while it changes). */
static uint8_t  g_channel   = DEFAULT_CHANNEL;
static uint8_t  g_len       = DEFAULT_LEN;

/* Trigger address, in on-air (RAW view) order, MSB first per byte. */
static uint8_t  g_addr[DEFAULT_ADDR_LEN] = {0x0B, 0x5C, 0x8E, 0x03};
static uint8_t  g_addr_len  = DEFAULT_ADDR_LEN;
static bool     g_order_rev;             /* 'o': reverse the byte sequence */
static bool     g_bit_rev   = true;      /* 'r': bit-reverse every byte */

/* Values actually written to the address registers, for 'h'. */
static uint32_t g_base0_written;
static uint32_t g_prefix0_written;
static uint8_t  g_balen_written;

static bool     g_capturing;             /* RX running, packets are pushed */
static bool     g_hfxo_ok;

/* On-air data rate ('m'), used for RX and TX.  Nrf_250Kbit is deprecated on
 * the nRF52840; it is only offered when the MDK still defines it. */
enum data_rate {
	RATE_1M,
	RATE_2M,
	RATE_250K,
	RATE_BLE1M,   /* 1 Mbit, +/-250 kHz deviation (vs +/-170 kHz for Nrf_1Mbit) */
	RATE_BLE2M,   /* 2 Mbit, +/-500 kHz deviation */
};
static uint8_t  g_rate      = RATE_1M;

/* Carrier lead-in before a 'z' burst ('j'): the radio sits in TXIDLE for
 * this long, emitting DTX (B1 by default), then START follows with no gap. */
static uint32_t g_tx_lead_us;
static uint8_t  g_tx_lead_dtx = RADIO_MODECNF0_DTX_B1;

/* Double buffer bookkeeping.  g_cur is the buffer the *next* reception uses;
 * it is advanced in the ADDRESS interrupt, so the buffer that just finished is
 * always (g_cur ^ 1). */
static uint8_t  g_rx_buf[RX_BUFS][PAYLOAD_MAX] __aligned(4);
static volatile uint8_t  g_cur;
static volatile bool     g_rx_running;

/* 32 bit capture values are extended to 64 bit by watching for wraps.  A gap
 * of more than 2^30 us (~18 min) between two address events would be needed
 * to confuse this. */
static volatile uint32_t g_cap_last;
static volatile uint64_t g_cap_high;
static volatile uint64_t g_isr_ts;        /* timestamp of the packet in flight */
static volatile uint64_t g_isr_prev_ts;   /* timestamp of the last packet out */

/* Packet ring (single producer = RADIO ISR, single consumer = main thread). */
struct pkt_entry {
	uint64_t t_us;
	uint32_t dt_us;
	int8_t   rssi;
	uint8_t  channel;
	uint8_t  len;
	uint8_t  data[PAYLOAD_MAX];
};

static struct pkt_entry g_pkt_ring[PKT_RING_LEN];
static volatile uint16_t g_ring_head;
static volatile uint16_t g_ring_tail;
static volatile uint32_t g_pkt_count;
static volatile uint32_t g_ring_drops;

/* Last captured frame, kept for 'x' replay. */
static uint8_t  g_last_frame[PAYLOAD_MAX];
static uint8_t  g_last_len;
static uint8_t  g_last_channel;
static bool     g_have_last;

/* Loaded TX payload segments for 'z' ('t1<hex>', 't2<hex>'); length 0 means
 * empty.  Each segment goes out as preamble + 'w' address + payload. */
#define TX_SEGS 2
static uint8_t  g_tx_seg[TX_SEGS][PAYLOAD_MAX] __aligned(4);
static uint8_t  g_tx_seg_len[TX_SEGS];

/* TX counters (totals since boot, shown by 'd'). */
static uint32_t g_tx_ready_total;        /* READY events seen in TX */
static uint32_t g_tx_end_total;          /* END events seen in TX */
static uint32_t g_tx_timeout_total;      /* timed-out TX event waits */
static int32_t  g_tx_gap_us;             /* last measured 'z' seg1->seg2 gap */
static bool     g_tx_gap_valid;

/* Console output: formatted lines are queued here and trickled into the CDC
 * ACM UART from the main loop, so a missing/slow host can never block us. */
static uint8_t        g_tx_area[TX_RING_BYTES];
static struct ring_buf g_tx_rb;
static uint32_t       g_tx_drops;
static uint32_t       g_ring_drops_reported;

static char   g_line[LINE_MAX];
static size_t g_line_len;
static bool   g_console_ok;

static const struct device *const console_dev =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

/* Energy scan results ('s'). */
static uint16_t g_scan_count[CHANNEL_MAX + 1];
static int8_t   g_scan_max[CHANNEL_MAX + 1];

/* Hop mode ('y'). */
struct hop_state {
	bool     active;
	uint8_t  chan[32];
	uint8_t  count;
	uint8_t  idx;
	uint8_t  fixed_channel;
	uint32_t dwell_ms;
	int64_t  last_ms;
};
static struct hop_state g_hop;

/* ------------------------------------------------------------------------- */
/* Small helpers                                                             */
/* ------------------------------------------------------------------------- */

static uint8_t bitrev8(uint8_t b)
{
	b = (uint8_t)(((b & 0xF0u) >> 4) | ((b & 0x0Fu) << 4));
	b = (uint8_t)(((b & 0xCCu) >> 2) | ((b & 0x33u) << 2));
	b = (uint8_t)(((b & 0xAAu) >> 1) | ((b & 0x55u) << 1));
	return b;
}

#if defined(RADIO_MODE_MODE_Nrf_250Kbit)
#define HAVE_RATE_250K 1
#else
#define HAVE_RATE_250K 0
#endif

static nrf_radio_mode_t rate_mode(void)
{
	switch (g_rate) {
	case RATE_2M:
		return NRF_RADIO_MODE_NRF_2MBIT;
	case RATE_BLE1M:
		return NRF_RADIO_MODE_BLE_1MBIT;
	case RATE_BLE2M:
		return NRF_RADIO_MODE_BLE_2MBIT;
#if HAVE_RATE_250K
	case RATE_250K:
		return NRF_RADIO_MODE_NRF_250KBIT;
#endif
	default:
		return NRF_RADIO_MODE_NRF_1MBIT;
	}
}

/* Air time of one byte in microseconds at the current rate. */
static uint32_t rate_us_per_byte(void)
{
	switch (g_rate) {
	case RATE_2M:
	case RATE_BLE2M:
		return 4u;
	case RATE_250K:
		return 32u;
	default:
		return 8u;
	}
}

static const char *rate_name(void)
{
	switch (g_rate) {
	case RATE_2M:
		return "Nrf_2Mbit";
	case RATE_BLE1M:
		return "Ble_1Mbit";
	case RATE_BLE2M:
		return "Ble_2Mbit";
	case RATE_250K:
		return "Nrf_250Kbit";
	default:
		return "Nrf_1Mbit";
	}
}

static void radio_irq_mask(void)
{
	irq_disable(RADIO_IRQn);
}

static void radio_irq_unmask(void)
{
	irq_enable(RADIO_IRQn);
}

/* Wait for a RADIO event without sleeping; the caller runs in thread context
 * or during early init.  Returns false on timeout (microseconds). */
static bool radio_wait_event(nrf_radio_event_t ev, uint32_t timeout_us)
{
	while (!nrf_radio_event_check(NRF_RADIO, ev)) {
		if (timeout_us == 0) {
			return false;
		}
		timeout_us--;
		k_busy_wait(1);
	}
	return true;
}

static bool radio_wait_disabled(uint32_t timeout_us)
{
	while (nrf_radio_state_get(NRF_RADIO) != NRF_RADIO_STATE_DISABLED) {
		if (timeout_us == 0) {
			return false;
		}
		timeout_us--;
		k_busy_wait(1);
	}
	return true;
}

/* Tight-poll a RADIO event for the TX paths that need low reaction latency.
 * The timeout is checked against the kernel cycle counter only every 32 polls,
 * so one poll is a single register read.  Safe with interrupts locked.
 * Returns false on timeout (microseconds, ~31 us resolution). */
static bool radio_spin_event(nrf_radio_event_t ev, uint32_t timeout_us)
{
	uint32_t start = k_cycle_get_32();
	uint32_t limit = k_us_to_cyc_ceil32(timeout_us) + 1u;

	while (true) {
		for (int i = 0; i < 32; i++) {
			if (nrf_radio_event_check(NRF_RADIO, ev)) {
				return true;
			}
		}
		if ((k_cycle_get_32() - start) > limit) {
			return nrf_radio_event_check(NRF_RADIO, ev);
		}
	}
}

/* Read the 1 MHz capture timer from thread context with the RADIO IRQ masked.
 * CC[0] is normally written by the PPI on every ADDRESS event; an explicit
 * capture task is used here, so this must not run while a reception is in
 * progress. */
static uint64_t timer_capture_now(void)
{
	nrf_timer_task_trigger(CAPTURE_TIMER, NRF_TIMER_TASK_CAPTURE0);

	uint32_t cap = CAPTURE_TIMER->CC[0];
	uint32_t last = g_cap_last;

	if ((cap < last) && ((last - cap) > 0x40000000u)) {
		g_cap_high += (1ull << 32);
	}
	g_cap_last = cap;

	return g_cap_high | cap;
}

/* ------------------------------------------------------------------------- */
/* Console output (never blocks, drops and counts when the host is absent)   */
/* ------------------------------------------------------------------------- */

static void line_reset(void)
{
	g_line_len = 0;
}

static void line_ch(char c)
{
	if (g_line_len < sizeof(g_line)) {
		g_line[g_line_len++] = c;
	}
}

static void line_str(const char *s)
{
	while (*s != '\0') {
		line_ch(*s++);
	}
}

static void line_u32(uint32_t v)
{
	char tmp[10];
	int i = 0;

	if (v == 0u) {
		line_ch('0');
		return;
	}
	while (v != 0u) {
		tmp[i++] = (char)('0' + (v % 10u));
		v /= 10u;
	}
	while (i > 0) {
		line_ch(tmp[--i]);
	}
}

static void line_i32(int32_t v)
{
	if (v < 0) {
		line_ch('-');
		line_u32((uint32_t)(-(v + 1)) + 1u); /* avoids -INT32_MIN UB */
	} else {
		line_u32((uint32_t)v);
	}
}

static void line_u64(uint64_t v)
{
	char tmp[20];
	int i = 0;

	if (v == 0u) {
		line_ch('0');
		return;
	}
	while (v != 0u) {
		tmp[i++] = (char)('0' + (uint8_t)(v % 10u));
		v /= 10u;
	}
	while (i > 0) {
		line_ch(tmp[--i]);
	}
}

static void line_hex_byte(uint8_t b)
{
	static const char hex[] = "0123456789ABCDEF";

	line_ch(hex[b >> 4]);
	line_ch(hex[b & 0x0Fu]);
}

static void line_hex(const uint8_t *data, uint16_t len)
{
	for (uint16_t i = 0; i < len; i++) {
		line_hex_byte(data[i]);
	}
}

/* Push the assembled line into the software output ring.  A line that does not
 * fit is dropped whole (a half line would confuse the laptop GUI) and counted,
 * never waited for. */
static void line_flush(void)
{
	if (ring_buf_space_get(&g_tx_rb) < (uint32_t)g_line_len) {
		g_tx_drops += (uint32_t)g_line_len;
		g_line_len = 0;
		return;
	}
	(void)ring_buf_put(&g_tx_rb, (const uint8_t *)g_line, (uint32_t)g_line_len);
	g_line_len = 0;
}

static void out_line(const char *s)
{
	line_reset();
	line_str(s);
	line_str("\r\n");
	line_flush();
}

/* Move queued output into the CDC ACM driver.  The USB device_next CDC ACM
 * driver only schedules a USB transfer from uart_poll_out() (uart_fifo_fill()
 * merely fills its ring and relies on the TX interrupt path), so bytes go out
 * through poll_out.  With flow control off, poll_out never sleeps: a full
 * driver ring discards the byte.  TX_PUMP_BUDGET bytes per main-loop pass
 * (1 ms) keeps that from happening at realistic packet rates. */
#define TX_PUMP_BUDGET 512
static void tx_pump(void)
{
	uint8_t  chunk[TX_CHUNK_BYTES];
	uint32_t budget = TX_PUMP_BUDGET;

	if (!g_console_ok) {
		return;
	}

	while (budget > 0u) {
		uint32_t have = ring_buf_get(&g_tx_rb, chunk,
					     MIN(sizeof(chunk), budget));

		if (have == 0u) {
			return;
		}
		for (uint32_t i = 0; i < have; i++) {
			uart_poll_out(console_dev, chunk[i]);
		}
		budget -= have;
	}
}

/* ------------------------------------------------------------------------- */
/* Radio configuration                                                       */
/* ------------------------------------------------------------------------- */

static bool hfxo_running(void)
{
	uint32_t stat = NRF_CLOCK->HFCLKSTAT;

	return ((stat & CLOCK_HFCLKSTAT_STATE_Msk) != 0u) &&
	       ((stat & CLOCK_HFCLKSTAT_SRC_Msk) ==
		(CLOCK_HFCLKSTAT_SRC_Xtal << CLOCK_HFCLKSTAT_SRC_Pos));
}

/* Request the 32 MHz crystal through Zephyr's clock driver and hold the
 * request forever.  Poking HFCLKSTART directly races the driver: its ISR eats
 * the HFCLKSTARTED event, and it stops the crystal when its own users (USB)
 * release it, which would leave the radio on the inaccurate RC oscillator. */
static struct onoff_client g_hfxo_cli;

static void hfxo_start(void)
{
	struct onoff_manager *mgr =
		z_nrf_clock_control_get_onoff(CLOCK_CONTROL_NRF_SUBSYS_HF);
	int res = 0;

	sys_notify_init_spinwait(&g_hfxo_cli.notify);
	if (onoff_request(mgr, &g_hfxo_cli) < 0) {
		g_hfxo_ok = false;
		return;
	}
	for (int i = 0; i < 2000; i++) { /* up to ~20 ms */
		if (sys_notify_fetch_result(&g_hfxo_cli.notify, &res) == 0) {
			break;
		}
		k_busy_wait(10);
	}
	g_hfxo_ok = hfxo_running();
}

/* Program BASE0/PREFIX0 from the user supplied on-air address.  See the long
 * comment at the top of the file for the reasoning behind the default. */
static void radio_apply_address(void)
{
	uint8_t bytes[5];
	uint32_t base = 0u;
	uint32_t prefix = 0u;
	uint8_t balen = (uint8_t)(g_addr_len - 1u);

	for (uint8_t i = 0; i < g_addr_len; i++) {
		bytes[i] = g_order_rev ? g_addr[g_addr_len - 1u - i] : g_addr[i];
	}
	if (g_bit_rev) {
		for (uint8_t i = 0; i < g_addr_len; i++) {
			bytes[i] = bitrev8(bytes[i]);
		}
	}

	/* The radio drops BASE0's low bytes when BALEN < 4 ("truncated from the
	 * least significant byte"), so the used base bytes sit at the top:
	 * first on-air byte -> lowest *used* byte, i.e. BASE0 byte (4 - BALEN).
	 * The last on-air byte is the prefix.  Matches Nordic ESB's addr_conv(). */
	for (uint8_t i = 0; i < balen; i++) {
		base |= (uint32_t)bytes[i] << (8u * (i + 4u - balen));
	}
	prefix = (uint32_t)bytes[g_addr_len - 1u];

	nrf_radio_base0_set(NRF_RADIO, base);
	nrf_radio_prefix0_set(NRF_RADIO, prefix);
	nrf_radio_rxaddresses_set(NRF_RADIO, 1u << 0); /* logical address 0 */
	nrf_radio_txaddress_set(NRF_RADIO, 0);

	g_base0_written = base;
	g_prefix0_written = prefix;
	g_balen_written = balen;
}

/* PCNF0/PCNF1: no S0/LENGTH/S1, fixed length payload, no whitening, big endian
 * (MSB first on air inside each payload byte, i.e. the nRF24 RAW view).
 * The address bits are always LSBit first regardless of ENDIAN. */
static void radio_apply_packet_format(uint8_t len)
{
	nrf_radio_packet_conf_t conf = {
		.lflen = 0,
		.s0len = 0,
		.s1len = 0,
		.s1incl = false,
		.cilen = 0,
		.plen = NRF_RADIO_PREAMBLE_LENGTH_8BIT,
		.crcinc = false,
		.termlen = 0,
		.maxlen = len,
		.statlen = len,
		.balen = (uint8_t)(g_addr_len - 1u),
		.big_endian = true,
		.whiteen = false,
	};

	nrf_radio_packet_configure(NRF_RADIO, &conf);

	/* CRC off, as on the nRF24 sniffer: every address hit ends a packet. */
	nrf_radio_crc_configure(NRF_RADIO, 0, NRF_RADIO_CRC_ADDR_INCLUDE, 0);
}

static void radio_apply_frequency(void)
{
	/* FREQUENCY is in MHz: 2400 + channel. */
	nrf_radio_frequency_set(NRF_RADIO, (uint16_t)(2400u + g_channel));
}

/* Full (re)configuration; the receiver must be stopped. */
static void radio_configure(void)
{
	radio_irq_mask();
	nrf_radio_mode_set(NRF_RADIO, rate_mode());
	nrf_radio_txpower_set(NRF_RADIO, NRF_RADIO_TXPOWER_0DBM);
	radio_apply_frequency();
	radio_apply_packet_format(g_len);
	radio_apply_address();
	nrf_radio_shorts_set(NRF_RADIO, 0);
	nrf_radio_int_enable(NRF_RADIO, RADIO_INT_MASK);
	radio_irq_unmask();
}

static void timer_capture_init(void)
{
	nrf_timer_task_trigger(CAPTURE_TIMER, NRF_TIMER_TASK_STOP);
	nrf_timer_mode_set(CAPTURE_TIMER, NRF_TIMER_MODE_TIMER);
	nrf_timer_bit_width_set(CAPTURE_TIMER, NRF_TIMER_BIT_WIDTH_32);
	nrf_timer_prescaler_set(CAPTURE_TIMER, CAPTURE_PRESCALE);
	nrf_timer_task_trigger(CAPTURE_TIMER, NRF_TIMER_TASK_CLEAR);
	nrf_timer_task_trigger(CAPTURE_TIMER, NRF_TIMER_TASK_START); /* free run */

	/* RADIO EVENTS_ADDRESS -> TIMER1 TASKS_CAPTURE[0].  The capture value is
	 * read in the ADDRESS interrupt; no timer interrupt is used. */
	nrf_ppi_channel_endpoint_setup(NRF_PPI, CAPTURE_PPI_CH,
				       (uint32_t)(uintptr_t)&NRF_RADIO->EVENTS_ADDRESS,
				       (uint32_t)(uintptr_t)&CAPTURE_TIMER->TASKS_CAPTURE[0]);
	nrf_ppi_channel_enable(NRF_PPI, CAPTURE_PPI_CH);
}

/* ------------------------------------------------------------------------- */
/* RADIO interrupt                                                           */
/* ------------------------------------------------------------------------- */

/* Copy one finished buffer into the ring.  Runs in the RADIO ISR. */
static void push_packet(uint8_t idx, uint64_t ts, int8_t rssi)
{
	uint16_t next = (uint16_t)((g_ring_head + 1u) % PKT_RING_LEN);
	uint32_t dt;

	if (next == g_ring_tail) {
		g_ring_drops++;
		g_isr_prev_ts = ts;
		return;
	}

	dt = (uint32_t)(ts - g_isr_prev_ts);
	g_pkt_ring[g_ring_head].t_us = ts;
	g_pkt_ring[g_ring_head].dt_us = dt;
	g_pkt_ring[g_ring_head].rssi = rssi;
	g_pkt_ring[g_ring_head].channel = g_channel;
	g_pkt_ring[g_ring_head].len = g_len;
	memcpy(g_pkt_ring[g_ring_head].data, g_rx_buf[idx], g_len);
	g_ring_head = next;

	g_pkt_count++;
	g_isr_prev_ts = ts;
}

static volatile uint32_t g_isr_calls, g_isr_addr, g_isr_end, g_isr_dis;

static void radio_isr(const void *arg)
{
	ARG_UNUSED(arg);
	g_isr_calls++;
	if (nrf_radio_event_check(NRF_RADIO, NRF_RADIO_EVENT_ADDRESS)) {
		g_isr_addr++;
	}
	if (nrf_radio_event_check(NRF_RADIO, NRF_RADIO_EVENT_END)) {
		g_isr_end++;
	}
	if (nrf_radio_event_check(NRF_RADIO, NRF_RADIO_EVENT_DISABLED)) {
		g_isr_dis++;
	}

	/* END first: if a new ADDRESS is already pending, g_cur has not been
	 * flipped for it yet, so (g_cur ^ 1) is still the buffer that ended. */
	if (nrf_radio_event_check(NRF_RADIO, NRF_RADIO_EVENT_END)) {
		uint8_t done = (uint8_t)(g_cur ^ 1u);
		int8_t rssi = (int8_t)-(int)nrf_radio_rssi_sample_get(NRF_RADIO); /* RSSISAMPLE is -dBm */

		nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);
		push_packet(done, g_isr_ts, rssi);
	}

	if (nrf_radio_event_check(NRF_RADIO, NRF_RADIO_EVENT_ADDRESS)) {
		uint32_t cap = CAPTURE_TIMER->CC[0]; /* latched by PPI */
		uint32_t last = g_cap_last;

		nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_ADDRESS);

		if ((cap < last) && ((last - cap) > 0x40000000u)) {
			g_cap_high += (1ull << 32);
		}
		g_cap_last = cap;
		g_isr_ts = g_cap_high | cap;

		/* PACKETPTR is latched by START: hand the *next* reception the
		 * other buffer, the one in flight keeps writing the old one. */
		g_cur = (uint8_t)(g_cur ^ 1u);
		nrf_radio_packetptr_set(NRF_RADIO, g_rx_buf[g_cur]);
	}

	/* No address match (or an aborted reception) never reaches END; the radio
	 * aborts into DISABLED, so re-enable reception here.  TASKS_RXEN is only
	 * valid from the DISABLED state, which is exactly where a pending DISABLED
	 * event leaves us; READY_START then re-arms the receiver. */
	if (nrf_radio_event_check(NRF_RADIO, NRF_RADIO_EVENT_DISABLED)) {
		nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_DISABLED);
		if (g_rx_running) {
			nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_RXEN);
		}
	}
}

/* ------------------------------------------------------------------------- */
/* Capture control                                                           */
/* ------------------------------------------------------------------------- */

static void radio_start_rx(void)
{
	radio_irq_mask();

	/* READY_START ramps up on RXEN and starts automatically, END_START
	 * re-arms after every good packet, ADDRESS_RSSISTART / DISABLED_RSSISTOP
	 * bracket the RSSI measurement. */
	nrf_radio_shorts_set(NRF_RADIO,
			     NRF_RADIO_SHORT_READY_START_MASK |
			     NRF_RADIO_SHORT_ADDRESS_RSSISTART_MASK |
			     NRF_RADIO_SHORT_END_START_MASK |
			     NRF_RADIO_SHORT_DISABLED_RSSISTOP_MASK);

	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_READY);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_ADDRESS);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_DISABLED);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_RSSIEND);

	g_cur = 0;
	nrf_radio_packetptr_set(NRF_RADIO, g_rx_buf[0]);
	/* Gives the first packet a real dt instead of its absolute timestamp. */
	g_isr_prev_ts = timer_capture_now();
	g_rx_running = true;
	nrf_radio_int_enable(NRF_RADIO, RADIO_INT_MASK);
	nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_RXEN);

	radio_irq_unmask();
	g_capturing = true;
}

static void radio_stop_rx(void)
{
	radio_irq_mask();

	g_rx_running = false;
	g_capturing = false;
	nrf_radio_shorts_set(NRF_RADIO, 0);
	nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_DISABLE);
	(void)radio_wait_disabled(1000);
	/* Drop anything a reception in flight may have latched. */
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_ADDRESS);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_DISABLED);

	radio_irq_unmask();
}

/* Change channel without losing more than the packet in flight. */
static void radio_retune(void)
{
	bool was_capturing = g_capturing;

	if (was_capturing) {
		radio_stop_rx();
	}
	radio_irq_mask();
	radio_apply_frequency();
	radio_irq_unmask();
	if (was_capturing) {
		radio_start_rx();
	}
}

/* Re-apply length / address, keeping the capture state. */
static void radio_reconfigure(void)
{
	bool was_capturing = g_capturing;

	if (was_capturing) {
		radio_stop_rx();
	}
	radio_irq_mask();
	radio_apply_packet_format(g_len);
	radio_apply_address();
	radio_apply_frequency();
	radio_irq_unmask();
	if (was_capturing) {
		radio_start_rx();
	}
}

/* ------------------------------------------------------------------------- */
/* Packet printing (main thread)                                             */
/* ------------------------------------------------------------------------- */

static void print_packet(const struct pkt_entry *p)
{
	line_reset();
	line_str("P ");
	line_u64(p->t_us);
	line_str(" dt=");
	line_u32(p->dt_us);
	line_str(" ch=");
	line_u32(p->channel);
	line_str(" rssi=");
	line_i32((int32_t)p->rssi);
	line_str(" RAW ");
	line_hex(p->data, p->len);
	line_str(" | REV ");
	for (uint16_t i = 0; i < p->len; i++) {
		line_hex_byte(bitrev8(p->data[i]));
	}
	line_str("\r\n");
	line_flush();
}

/* Drain the packet ring; returns the number of printed lines. */
static uint16_t drain_packets(uint16_t budget)
{
	uint16_t printed = 0;

	while (g_ring_tail != g_ring_head && printed < budget) {
		const struct pkt_entry *p = &g_pkt_ring[g_ring_tail];

		print_packet(p);

		/* Keep the newest frame for 'x' replay. */
		g_last_len = p->len;
		g_last_channel = p->channel;
		memcpy(g_last_frame, p->data, p->len);
		g_have_last = true;

		g_ring_tail = (uint16_t)((g_ring_tail + 1u) % PKT_RING_LEN);
		printed++;
	}

	return printed;
}

/* Report lost packets at most a few times per second, so the report itself
 * cannot swamp the link. */
static void report_drops(int64_t now_ms)
{
	static int64_t last_ms;

	uint32_t lost = g_ring_drops - g_ring_drops_reported;

	if (lost == 0u || (now_ms - last_ms) < 100) {
		return;
	}
	last_ms = now_ms;
	g_ring_drops_reported = g_ring_drops;

	line_reset();
	line_str("[drop ");
	line_u32(lost);
	line_str("]");
	line_str("\r\n");
	line_flush();
}

/* ------------------------------------------------------------------------- */
/* Help / configuration dump                                                 */
/* ------------------------------------------------------------------------- */

static void print_config(void)
{
	line_reset();
	line_str("channel       : ");
	line_u32(g_channel);
	line_str(" (");
	line_u32(2400u + g_channel);
	line_str(" MHz)\r\n");
	line_flush();

	line_reset();
	line_str("data rate     : ");
	line_str(rate_name());
	line_str(" (");
	line_u32(rate_us_per_byte());
	line_str(" us/byte, RX and TX)\r\n");
	line_flush();

	line_reset();
	line_str("payload len   : ");
	line_u32(g_len);
	line_str("\r\n");
	line_flush();

	line_reset();
	line_str("trigger addr  : ");
	line_hex(g_addr, g_addr_len);
	line_str(" (");
	line_u32(g_addr_len);
	line_str(" bytes, on-air order)\r\n");
	line_flush();

	line_reset();
	line_str("byte order    : ");
	line_str(g_order_rev ? "reversed\r\n" : "as given\r\n");
	line_flush();

	line_reset();
	line_str("bit reversal  : ");
	line_str(g_bit_rev ? "on\r\n" : "off\r\n");
	line_flush();

	line_reset();
	line_str("BALEN         : ");
	line_u32(g_balen_written);
	line_str(" (address = BALEN + 1 prefix byte)\r\n");
	line_flush();

	line_reset();
	line_str("BASE0         : 0x");
	for (int shift = 24; shift >= 0; shift -= 8) {
		line_hex_byte((uint8_t)((g_base0_written >> shift) & 0xFFu));
	}
	line_str("\r\n");
	line_flush();

	line_reset();
	line_str("PREFIX0       : 0x");
	line_hex_byte((uint8_t)(g_prefix0_written & 0xFFu));
	line_str(" (AP0, most significant address byte)\r\n");
	line_flush();

	line_reset();
	line_str("HFXO          : ");
	line_str(hfxo_running() ? "running\r\n" : "NOT RUNNING\r\n");
	line_flush();

	line_reset();
	line_str("capture       : ");
	line_str(g_capturing ? "on" : "off");
	line_str(g_hop.active ? " (hopping)\r\n" : "\r\n");
	line_flush();

	line_reset();
	line_str("packets       : ");
	line_u32(g_pkt_count);
	line_str("\r\n");
	line_flush();

	line_reset();
	line_str("ring drops    : ");
	line_u32(g_ring_drops);
	line_str("\r\n");
	line_flush();

	line_reset();
	line_str("output drops  : ");
	line_u32(g_tx_drops);
	line_str(" bytes\r\n");
	line_flush();

	for (uint8_t s = 0; s < TX_SEGS; s++) {
		line_reset();
		line_str("tx seg");
		line_u32(s + 1u);
		line_str("       : ");
		if (g_tx_seg_len[s] == 0u) {
			line_str("empty\r\n");
		} else {
			line_u32(g_tx_seg_len[s]);
			line_str(" bytes ");
			line_hex(g_tx_seg[s], g_tx_seg_len[s]);
			line_str("\r\n");
		}
		line_flush();
	}

	if (g_hop.active) {
		line_reset();
		line_str("hop           : ");
		line_u32(g_hop.count);
		line_str(" channels @ ");
		line_u32(g_hop.dwell_ms);
		line_str(" ms: ");
		for (uint8_t i = 0; i < g_hop.count; i++) {
			if (i != 0u) {
				line_ch(',');
			}
			line_u32(g_hop.chan[i]);
		}
		line_str("\r\n");
		line_flush();
	}
}

static void print_help(void)
{
	out_line("");
	out_line("=== chilipad-sniffer (nRF52840 dongle, LT8920 2.4 GHz) ===");
	out_line("h            help + current config");
	out_line("p            start/stop capture (starts OFF at boot)");
	out_line("c<n>         channel 0..100 (default 50 = 2450 MHz)");
	out_line("l<n>         payload length 1..255 (default 64)");
	out_line("w<hex>       trigger address, 3..5 bytes on-air order (0B5C8E03)");
	out_line("             'w' alone resets the address to the default");
	out_line("o            toggle byte order (as given <-> reversed)");
	out_line("r            toggle per-byte bit reversal (on by default)");
	out_line("s<sec>       energy scan ch 0..100 for <sec> s (default 2, max 60)");
	out_line("e<ms>        energy vs time on channel c for <ms> (default 2000, max 30000)");
	out_line("y<list>@<ms> hop channels, e.g. y10,30,50,70@200; 'y' stops");
	out_line("x<n>         replay the last captured frame n times (default 10)");
	out_line("t<hex>       load TX segment 1 (up to 255 bytes); same as t1<hex>");
	out_line("t1<hex>      load TX segment 1; 't1' alone clears it");
	out_line("t2<hex>      load TX segment 2; 't2' alone clears it");
	out_line("             't' alone clears both segments");
	out_line("z<n>         send seg1 (+ seg2 back-to-back) n times, 1..1000");
	out_line("             (default 1), each seg = preamble + 'w' addr + payload");
	out_line("m<rate>      data rate for RX and TX: m1 = 1 Mbit (default), m2 = 2 Mbit");
	out_line(HAVE_RATE_250K ? "             m250 = 250 kbit (deprecated on nRF52840)"
				: "             (m250 = 250 kbit: not in this MDK, rejected)");
	out_line("j<ms>[b0|b1|c] carrier lead-in before each z burst, 0 = off (b1 default)");
	out_line("k<ms>        unmodulated carrier for 1..5000 ms on channel c;");
	out_line("             k<ms>b0 / k<ms>b1 = constant '0' / '1' tone (DTX B0/B1)");
	out_line("d            radio register dump + ISR and TX counters");
	out_line("");
	print_config();
}

/* ------------------------------------------------------------------------- */
/* Energy scan ('s')                                                         */
/* ------------------------------------------------------------------------- */

static void energy_scan(uint32_t seconds)
{
	bool was_capturing = g_capturing;
	int64_t deadline;
	uint32_t sweeps = 0;

	if (seconds == 0u) {
		seconds = 2;
	}
	if (seconds > 60u) {
		seconds = 60;
	}

	if (was_capturing) {
		radio_stop_rx();
	}
	/* Keep the RADIO interrupt out of the way for the whole sweep: no packet
	 * pushes, no PACKETPTR games, only RSSI.  INTEN is switched off as well so
	 * no stale ADDRESS/END event survives the sweep. */
	radio_irq_mask();
	nrf_radio_int_disable(NRF_RADIO, RADIO_INT_MASK);
	nrf_radio_shorts_set(NRF_RADIO, 0);

	memset(g_scan_count, 0, sizeof(g_scan_count));
	for (uint16_t ch = 0; ch <= CHANNEL_MAX; ch++) {
		g_scan_max[ch] = -128;
	}

	line_reset();
	line_str("[scan] ");
	line_u32(seconds);
	line_str(" s RSSI sweep over ch 0..100, press remote buttons...\r\n");
	line_flush();
	tx_pump();

	deadline = k_uptime_get() + (int64_t)seconds * 1000;

	while (k_uptime_get() < deadline) {
		for (uint16_t ch = 0; ch <= CHANNEL_MAX; ch++) {
			nrf_radio_frequency_set(NRF_RADIO, (uint16_t)(2400u + ch));
			nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_READY);
			nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_RSSIEND);
			nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_DISABLED);

			/* RXEN ramps up to RXIDLE (READY); RSSISTART/RSSIEND then takes a
			 * single carrier level sample while nothing is being received. */
			nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_RXEN);
			if (!radio_wait_event(NRF_RADIO_EVENT_READY, 200u)) {
				/* Leave the radio disabled so the next channel can RXEN. */
				nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_DISABLE);
				(void)radio_wait_disabled(200);
				continue;
			}

			for (int sample = 0; sample < 8; sample++) {
				nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_RSSIEND);
				nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_RSSISTART);
				if (!radio_wait_event(NRF_RADIO_EVENT_RSSIEND, 200u)) {
					break;
				}

				int8_t rssi = (int8_t)-(int)nrf_radio_rssi_sample_get(NRF_RADIO); /* RSSISAMPLE is -dBm */

				if (rssi > g_scan_max[ch]) {
					g_scan_max[ch] = rssi;
				}
				if (rssi > -75) {
					g_scan_count[ch]++;
				}
			}

			nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_DISABLE);
			(void)radio_wait_disabled(200);
		}
		sweeps++;

		/* Keep the console alive during long scans. */
		tx_pump();
	}

	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_DISABLED);

	/* Every channel, in channel order, so two scans (idle vs. button presses)
	 * can be diffed on the host: "S <ch> <count above -75 dBm> <max dBm>". */
	line_reset();
	line_str("[scan] ");
	line_u32(sweeps);
	line_str(" sweeps; S <ch> <samples above -75 dBm> <max dBm>\r\n");
	line_flush();
	for (uint16_t ch = 0; ch <= CHANNEL_MAX; ch++) {
		line_reset();
		line_str("S ");
		line_u32(ch);
		line_ch(' ');
		line_u32(g_scan_count[ch]);
		line_ch(' ');
		line_i32((int32_t)g_scan_max[ch]);
		line_str("\r\n");
		line_flush();
		tx_pump();
	}
	out_line("[scan] done");

	/* Back to the configured channel and capture state. */
	radio_apply_frequency();
	radio_apply_packet_format(g_len);
	radio_apply_address();
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_ADDRESS);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_DISABLED);
	nrf_radio_int_enable(NRF_RADIO, RADIO_INT_MASK);
	radio_irq_unmask();

	if (was_capturing) {
		radio_start_rx();
	}
}

/* ------------------------------------------------------------------------- */
/* Energy vs. time on the current channel ('e')                              */
/* ------------------------------------------------------------------------- */

#define ENERGY_SEGS   128
#define ENERGY_THRESH (-62)   /* dBm; WiFi here stays below -60 */

/* Samples RSSI back-to-back (about 10 us per sample) for <ms> and prints
 * every stretch above ENERGY_THRESH as "E <start_us> <dur_us> <max_dBm>".
 * Unmodulated carrier counts as energy, so this shows the lead-in before a
 * burst and any repeats, which an address trigger cannot. */
static void energy_time(uint32_t ms)
{
	bool was_capturing = g_capturing;
	static uint32_t seg_start[ENERGY_SEGS], seg_end[ENERGY_SEGS];
	static int8_t   seg_max[ENERGY_SEGS];
	uint32_t nseg = 0;
	bool in_seg = false;
	uint32_t below = 0;
	uint32_t samples = 0;
	int8_t cur_max = -128;
	uint32_t t_last_above = 0;

	if (ms == 0u) {
		ms = 2000;
	}
	if (ms > 30000u) {
		ms = 30000;
	}
	if (was_capturing) {
		radio_stop_rx();
	}
	radio_irq_mask();
	nrf_radio_int_disable(NRF_RADIO, RADIO_INT_MASK);
	nrf_radio_shorts_set(NRF_RADIO, 0);
	radio_apply_frequency();

	line_reset();
	line_str("[e] energy vs time on ch ");
	line_u32(g_channel);
	line_str(" for ");
	line_u32(ms);
	line_str(" ms, threshold ");
	line_i32(ENERGY_THRESH);
	line_str(" dBm; press the remote...\r\n");
	line_flush();
	tx_pump();

	nrf_timer_task_trigger(CAPTURE_TIMER, NRF_TIMER_TASK_STOP);
	nrf_timer_task_trigger(CAPTURE_TIMER, NRF_TIMER_TASK_CLEAR);
	nrf_timer_task_trigger(CAPTURE_TIMER, NRF_TIMER_TASK_START);

	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_READY);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_DISABLED);
	nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_RXEN);
	if (!radio_wait_event(NRF_RADIO_EVENT_READY, 1000u)) {
		out_line("[e] READY timeout");
		goto done;
	}

	for (;;) {
		nrf_timer_task_trigger(CAPTURE_TIMER, NRF_TIMER_TASK_CAPTURE1);
		uint32_t now = CAPTURE_TIMER->CC[1];

		if (now >= ms * 1000u) {
			break;
		}
		nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_RSSIEND);
		nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_RSSISTART);
		if (!radio_wait_event(NRF_RADIO_EVENT_RSSIEND, 200u)) {
			continue;
		}
		int8_t rssi = (int8_t)-(int)nrf_radio_rssi_sample_get(NRF_RADIO);
		samples++;

		if (rssi >= ENERGY_THRESH) {
			if (!in_seg) {
				if (nseg >= ENERGY_SEGS) {
					break;
				}
				in_seg = true;
				seg_start[nseg] = now;
				cur_max = rssi;
			} else if (rssi > cur_max) {
				cur_max = rssi;
			}
			t_last_above = now;
			below = 0;
		} else if (in_seg) {
			/* End a segment after ~30 us (3 samples) of silence so the short
			 * dips inside a burst do not split it. */
			if (++below >= 3u) {
				seg_end[nseg] = t_last_above;
				seg_max[nseg] = cur_max;
				nseg++;
				in_seg = false;
			}
		}
	}
	if (in_seg && nseg < ENERGY_SEGS) {
		seg_end[nseg] = t_last_above;
		seg_max[nseg] = cur_max;
		nseg++;
	}

done:
	nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_DISABLE);
	(void)radio_wait_disabled(1000);

	line_reset();
	line_str("[e] ");
	line_u32(samples);
	line_str(" samples, ");
	line_u32(nseg);
	line_str(" stretches above threshold; E <start_us> <dur_us> <max_dBm>\r\n");
	line_flush();
	for (uint32_t i = 0; i < nseg; i++) {
		line_reset();
		line_str("E ");
		line_u32(seg_start[i]);
		line_ch(' ');
		line_u32(seg_end[i] - seg_start[i]);
		line_ch(' ');
		line_i32((int32_t)seg_max[i]);
		line_str("\r\n");
		line_flush();
		tx_pump();
	}
	out_line("[e] done");

	radio_apply_packet_format(g_len);
	radio_apply_address();
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_ADDRESS);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_DISABLED);
	nrf_radio_int_enable(NRF_RADIO, RADIO_INT_MASK);
	radio_irq_unmask();
	if (was_capturing) {
		radio_start_rx();
	}
}

/* ------------------------------------------------------------------------- */
/* Replay ('x')                                                              */
/* ------------------------------------------------------------------------- */

/* Back to the receive configuration after a TX run ('x' / 'z'). */
static void radio_tx_restore_rx(bool was_capturing)
{
	radio_apply_packet_format(g_len);
	radio_apply_frequency();
	nrf_radio_int_enable(NRF_RADIO, RADIO_INT_MASK);
	radio_irq_unmask();

	if (was_capturing) {
		radio_start_rx();
	}
}

/* "[tx] sent=<n> segs=<m> timeouts=<k>": complete frames/bursts, segment END
 * events and timed-out event waits of the run that just finished. */
static void print_tx_summary(uint32_t sent, uint32_t segs, uint32_t timeouts)
{
	line_reset();
	line_str("[tx] sent=");
	line_u32(sent);
	line_str(" segs=");
	line_u32(segs);
	line_str(" timeouts=");
	line_u32(timeouts);
	line_str("\r\n");
	line_flush();
}

static void replay_frame(uint32_t times)
{
	bool was_capturing = g_capturing;

	if (!g_have_last) {
		out_line("[x] no frame captured yet; press 'p' first");
		return;
	}
	if (times == 0u) {
		times = 10;
	}
	if (times > 1000u) {
		times = 1000;
	}

	if (was_capturing) {
		radio_stop_rx();
	}

	/* Interrupts stay off for the whole replay: a TX END event must not be
	 * mistaken for a received packet. */
	radio_irq_mask();
	nrf_radio_int_disable(NRF_RADIO, RADIO_INT_MASK);

	/* Same channel, address, length and no CRC as the RX path; only the
	 * direction changes.  STATLEN/MAXLEN follow the stored frame. */
	nrf_radio_frequency_set(NRF_RADIO, (uint16_t)(2400u + g_last_channel));
	radio_apply_packet_format(g_last_len);
	radio_apply_address();
	nrf_radio_txaddress_set(NRF_RADIO, 0);
	nrf_radio_shorts_set(NRF_RADIO, NRF_RADIO_SHORT_READY_START_MASK);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_READY);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);
	nrf_radio_packetptr_set(NRF_RADIO, g_last_frame);

	line_reset();
	line_str("[replay] ch=");
	line_u32(g_last_channel);
	line_str(" len=");
	line_u32(g_last_len);
	line_str(" addr=");
	line_hex(g_addr, g_addr_len);
	line_str(" x");
	line_u32(times);
	line_str(" RAW ");
	line_hex(g_last_frame, g_last_len);
	line_str("\r\n");
	line_flush();
	tx_pump();

	uint32_t sent = 0;
	uint32_t timeouts = 0;
	/* START -> END is preamble + address + payload; 5 ms covers 1 and 2 Mbit,
	 * 250 kbit needs up to ~8.3 ms. */
	uint32_t end_us = (1u + g_addr_len + (uint32_t)g_last_len) *
			  rate_us_per_byte() + 1000u;

	if (end_us < 5000u) {
		end_us = 5000u;
	}

	nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_TXEN);
	if (radio_wait_event(NRF_RADIO_EVENT_READY, 1000u)) {
		nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_READY);
		g_tx_ready_total++;
		for (uint32_t i = 0; i < times; i++) {
			if (i != 0u) {
				nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_START);
			}
			if (!radio_wait_event(NRF_RADIO_EVENT_END, end_us)) {
				timeouts++;
				break;
			}
			nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);
			g_tx_end_total++;
			sent++;
			k_msleep(5); /* ~5 ms between bursts */
		}
	} else {
		timeouts++;
	}
	g_tx_timeout_total += timeouts;

	nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_DISABLE);
	(void)radio_wait_disabled(1000);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_DISABLED);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);

	/* Restore the receive configuration. */
	radio_tx_restore_rx(was_capturing);
	print_tx_summary(sent, sent, timeouts);
}

/* ------------------------------------------------------------------------- */
/* Segment transmit ('t' / 'z')                                              */
/* ------------------------------------------------------------------------- */

/* Disable the radio after a 'z' repetition (or an aborted one) and drop every
 * event it may have latched. */
static void radio_tx_disable(void)
{
	nrf_radio_shorts_set(NRF_RADIO, 0);
	nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_DISABLE);
	(void)radio_wait_disabled(1000);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_READY);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_ADDRESS);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_DISABLED);
}

/* One repetition of the loaded segments.  Returns the number of segments
 * whose END was seen; *timeouts counts failed event waits.
 *
 * Equal lengths (or one segment): READY_START | END_START.  PACKETPTR is
 * double buffered and latched by START, so it is pointed at segment 2 after
 * segment 1's ADDRESS event; the END_START short then starts segment 2 in
 * hardware with no CPU in the path.  END_START is removed after segment 1's
 * END, long before segment 2 can end (it lasts at least preamble + address).
 *
 * Different lengths: PCNF1.MAXLEN/STATLEN must change between the segments.
 * The nRF52840 PS documents PACKETPTR as double buffered but says nothing of
 * the kind for PCNF1, and an END_START short fires START on the same event,
 * so there is no window to rewrite PCNF1 in between.  Instead END is polled
 * with all interrupts locked, PCNF1 is rewritten in TXIDLE and START is
 * triggered by the CPU: a few CPU cycles of extra gap. */
static uint8_t tx_one_burst(uint32_t pcnf1_1, uint32_t pcnf1_2, bool two,
			    bool hw_chain, uint32_t *timeouts)
{
	uint8_t len1 = g_tx_seg_len[0];
	uint8_t segs = 0;
	uint32_t us_per_byte = rate_us_per_byte();
	uint32_t cap1;
	unsigned int key = 0;
	bool locked = false;

	NRF_RADIO->PCNF1 = pcnf1_1;
	nrf_radio_packetptr_set(NRF_RADIO, g_tx_seg[0]);
	/* With a lead-in ('j'), READY does not start the packet: the radio sits
	 * in TXIDLE emitting the DTX carrier, then START is triggered by the CPU
	 * so the carrier flows straight into the preamble. */
	nrf_radio_shorts_set(NRF_RADIO,
			     (g_tx_lead_us ? 0u : NRF_RADIO_SHORT_READY_START_MASK) |
			     (hw_chain ? NRF_RADIO_SHORT_END_START_MASK : 0u));
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_READY);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_ADDRESS);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_DISABLED);
	if (g_tx_lead_us) {
		nrf_radio_modecnf0_set(NRF_RADIO, nrf_radio_modecnf0_ru_get(NRF_RADIO),
				       g_tx_lead_dtx);
	}

	nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_TXEN);
	if (!radio_spin_event(NRF_RADIO_EVENT_READY, 1000u)) {
		goto timeout;
	}
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_READY);
	g_tx_ready_total++;
	if (g_tx_lead_us) {
		k_busy_wait(g_tx_lead_us);
		nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_START);
	}

	if (two) {
		/* From here to segment 1's END nothing may delay the CPU: the
		 * PACKETPTR switch must land before END (a 1 byte segment 1 leaves
		 * one byte time: 8 us at 1 Mbit, 4 us at 2 Mbit), and the CPU chain
		 * path starts segment 2 itself.  At most preamble + address + 255
		 * bytes: ~2.1 ms at 1 Mbit, ~1.0 ms at 2 Mbit, ~8.3 ms at 250 kbit. */
		key = irq_lock();
		locked = true;
	}

	if (!radio_spin_event(NRF_RADIO_EVENT_ADDRESS, 1000u)) {
		goto timeout;
	}
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_ADDRESS);
	cap1 = CAPTURE_TIMER->CC[0]; /* PPI: ADDRESS -> CAPTURE[0] */

	if (two) {
		/* Segment 1 is on air and its PACKETPTR is latched. */
		nrf_radio_packetptr_set(NRF_RADIO, g_tx_seg[1]);
	}

	if (!radio_spin_event(NRF_RADIO_EVENT_END,
			      (uint32_t)len1 * us_per_byte + 1000u)) {
		goto timeout;
	}
	if (two && !hw_chain) {
		NRF_RADIO->PCNF1 = pcnf1_2;
		nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_START);
	}
	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);
	nrf_radio_shorts_set(NRF_RADIO, 0);
	segs++;
	g_tx_end_total++;

	if (locked) {
		irq_unlock(key);
		locked = false;
	}

	if (two) {
		uint8_t len2 = g_tx_seg_len[1];

		if (!radio_spin_event(NRF_RADIO_EVENT_ADDRESS, 1000u)) {
			goto timeout;
		}
		nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_ADDRESS);

		/* ADDRESS -> ADDRESS spacing minus the nominal air time of
		 * segment 1's payload + segment 2's preamble and address
		 * (8 us per byte at 1 Mbit, 4 at 2 Mbit, 32 at 250 kbit) is
		 * the extra gap. */
		uint32_t nominal = ((uint32_t)len1 + 1u + g_addr_len) * us_per_byte;

		g_tx_gap_us = (int32_t)(CAPTURE_TIMER->CC[0] - cap1) -
			      (int32_t)nominal;
		g_tx_gap_valid = true;

		if (!radio_spin_event(NRF_RADIO_EVENT_END,
				      (uint32_t)len2 * us_per_byte + 1000u)) {
			goto timeout;
		}
		nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_END);
		segs++;
		g_tx_end_total++;
	}

	radio_tx_disable();
	return segs;

timeout:
	if (locked) {
		irq_unlock(key);
	}
	(*timeouts)++;
	radio_tx_disable();
	return segs;
}

static void tx_segments(uint32_t times)
{
	bool was_capturing = g_capturing;
	bool two = (g_tx_seg_len[1] != 0u);
	bool hw_chain;
	uint32_t pcnf1_1;
	uint32_t pcnf1_2;
	uint32_t sent = 0;
	uint32_t segs = 0;
	uint32_t timeouts = 0;

	if (g_tx_seg_len[0] == 0u) {
		out_line("[z] segment 1 is empty; load it with t<hex> or t1<hex>");
		return;
	}
	if (times == 0u) {
		times = 1;
	}
	if (times > 1000u) {
		times = 1000;
	}
	hw_chain = two && (g_tx_seg_len[0] == g_tx_seg_len[1]);
	g_tx_gap_valid = false;

	if (was_capturing) {
		radio_stop_rx();
	}

	/* As in replay: RADIO interrupt off for the whole run so a TX event is
	 * never taken for a received packet. */
	radio_irq_mask();
	nrf_radio_int_disable(NRF_RADIO, RADIO_INT_MASK);

	radio_apply_frequency();
	radio_apply_packet_format(g_tx_seg_len[0]);
	radio_apply_address();
	nrf_radio_txaddress_set(NRF_RADIO, 0);

	pcnf1_1 = NRF_RADIO->PCNF1;
	pcnf1_2 = pcnf1_1;
	if (two) {
		pcnf1_2 = (pcnf1_1 & ~(RADIO_PCNF1_MAXLEN_Msk | RADIO_PCNF1_STATLEN_Msk)) |
			  ((uint32_t)g_tx_seg_len[1] << RADIO_PCNF1_MAXLEN_Pos) |
			  ((uint32_t)g_tx_seg_len[1] << RADIO_PCNF1_STATLEN_Pos);
	}

	line_reset();
	line_str("[z] ch=");
	line_u32(g_channel);
	line_str(" addr=");
	line_hex(g_addr, g_addr_len);
	line_str(" len1=");
	line_u32(g_tx_seg_len[0]);
	if (two) {
		line_str(" len2=");
		line_u32(g_tx_seg_len[1]);
		line_str(hw_chain ? " chain=END_START" : " chain=cpu");
	}
	line_str(" x");
	line_u32(times);
	line_str("\r\n");
	line_flush();
	tx_pump();

	for (uint32_t i = 0; i < times; i++) {
		uint32_t to = 0;
		uint8_t done = tx_one_burst(pcnf1_1, pcnf1_2, two, hw_chain, &to);

		segs += done;
		timeouts += to;
		if (to != 0u) {
			break;
		}
		sent++;
		if (i + 1u < times) {
			k_msleep(5); /* ~5 ms between bursts, radio disabled */
		}
	}
	g_tx_timeout_total += timeouts;

	radio_tx_restore_rx(was_capturing);
	print_tx_summary(sent, segs, timeouts);
	if (two && g_tx_gap_valid && sent != 0u) {
		line_reset();
		line_str("[z] seg gap=");
		line_i32(g_tx_gap_us);
		line_str(" us beyond preamble+address (TIMER1, +/-1 us)\r\n");
		line_flush();
	}
}

/* ------------------------------------------------------------------------- */
/* Carrier ('k')                                                             */
/* ------------------------------------------------------------------------- */

/* Unmodulated carrier: with SHORTS = 0, TXEN ramps the radio up into TXIDLE
 * and stays there; MODECNF0.DTX chooses what TXIDLE sends (Center = carrier at
 * the channel frequency, B0 / B1 = the constant '0' / '1' FSK tone).  Capture
 * is stopped first and restored afterwards, as in replay. */
static void tx_carrier(uint32_t ms, uint8_t dtx)
{
	bool was_capturing = g_capturing;
	uint32_t saved_modecnf0;
	const char *name = (dtx == RADIO_MODECNF0_DTX_B0) ? "b0" :
			   (dtx == RADIO_MODECNF0_DTX_B1) ? "b1" : "center";
	bool ok;

	if (was_capturing) {
		radio_stop_rx();
	}

	radio_irq_mask();
	nrf_radio_int_disable(NRF_RADIO, RADIO_INT_MASK);
	nrf_radio_shorts_set(NRF_RADIO, 0);
	radio_apply_frequency();

	saved_modecnf0 = NRF_RADIO->MODECNF0;
	nrf_radio_modecnf0_set(NRF_RADIO, nrf_radio_modecnf0_ru_get(NRF_RADIO), dtx);

	line_reset();
	line_str("[k] carrier ");
	line_str(name);
	line_str(" ch=");
	line_u32(g_channel);
	line_str(" (");
	line_u32(2400u + g_channel);
	line_str(" MHz) ");
	line_u32(ms);
	line_str(" ms ");
	line_str(rate_name());
	line_str("\r\n");
	line_flush();
	tx_pump();

	nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_READY);
	nrf_radio_task_trigger(NRF_RADIO, NRF_RADIO_TASK_TXEN);
	ok = radio_wait_event(NRF_RADIO_EVENT_READY, 1000u);
	if (ok) {
		nrf_radio_event_clear(NRF_RADIO, NRF_RADIO_EVENT_READY);
		g_tx_ready_total++;
		k_msleep((int32_t)ms); /* radio sits in TXIDLE */
	} else {
		g_tx_timeout_total++;
	}

	radio_tx_disable();
	NRF_RADIO->MODECNF0 = saved_modecnf0;

	radio_tx_restore_rx(was_capturing);

	line_reset();
	if (ok) {
		line_str("[k] carrier ");
		line_str(name);
		line_ch(' ');
		line_u32(ms);
		line_str(" ms done\r\n");
	} else {
		line_str("[k] READY timeout, no carrier sent\r\n");
	}
	line_flush();
}

/* ------------------------------------------------------------------------- */
/* Command parser                                                            */
/* ------------------------------------------------------------------------- */

static bool parse_u32(const char *s, uint32_t *out)
{
	uint32_t value = 0;
	bool any = false;

	while (*s != '\0') {
		if (*s < '0' || *s > '9') {
			return false;
		}
		value = value * 10u + (uint32_t)(*s - '0');
		if (value > 1000000u) {
			return false; /* keeps the callers' range checks meaningful */
		}
		any = true;
		s++;
	}
	if (!any) {
		return false;
	}
	*out = value;
	return true;
}

static int hex_nibble(char c)
{
	if (c >= '0' && c <= '9') {
		return c - '0';
	}
	if (c >= 'a' && c <= 'f') {
		return c - 'a' + 10;
	}
	if (c >= 'A' && c <= 'F') {
		return c - 'A' + 10;
	}
	return -1;
}

/* "w<hex>": 3..5 bytes, exactly two hex digits per byte. */
static bool parse_hex_addr(const char *s, uint8_t *out, uint8_t *out_len)
{
	uint8_t bytes[5];
	uint8_t count = 0;

	while (*s != '\0') {
		int hi = hex_nibble(s[0]);
		int lo = (s[1] != '\0') ? hex_nibble(s[1]) : -1;

		if (hi < 0 || lo < 0 || count >= 5) {
			return false;
		}
		bytes[count++] = (uint8_t)((hi << 4) | lo);
		s += 2;
	}
	if (count < 3u || count > 5u) {
		return false;
	}
	memcpy(out, bytes, count);
	*out_len = count;
	return true;
}

static void cmd_set_channel(uint32_t ch)
{
	if (ch > CHANNEL_MAX) {
		out_line("[cfg] channel must be 0..100, unchanged");
		return;
	}
	g_channel = (uint8_t)ch;
	if (g_hop.active) {
		g_hop.active = false;
		out_line("[cfg] hopping stopped (fixed channel selected)");
	}
	radio_retune();

	line_reset();
	line_str("[cfg] channel -> ");
	line_u32(g_channel);
	line_str(" (");
	line_u32(2400u + g_channel);
	line_str(" MHz)\r\n");
	line_flush();
}

static void cmd_set_len(uint32_t len)
{
	if (len < 1u || len > PAYLOAD_MAX) {
		out_line("[cfg] length must be 1..255, unchanged");
		return;
	}
	g_len = (uint8_t)len;
	radio_reconfigure();

	line_reset();
	line_str("[cfg] payload length -> ");
	line_u32(g_len);
	line_str("\r\n");
	line_flush();
}

static void cmd_set_addr(const char *arg)
{
	if (*arg == '\0') {
		static const uint8_t def[DEFAULT_ADDR_LEN] = {0x0B, 0x5C, 0x8E, 0x03};

		memcpy(g_addr, def, sizeof(def));
		g_addr_len = DEFAULT_ADDR_LEN;
		radio_reconfigure();
		out_line("[addr] reset to 0B5C8E03 (4 bytes)");
	} else {
		uint8_t bytes[5];
		uint8_t len;

		if (!parse_hex_addr(arg, bytes, &len)) {
			out_line("[addr] usage: w<hex> = 3..5 bytes, e.g. w0B5C8E03");
			return;
		}
		memcpy(g_addr, bytes, len);
		g_addr_len = len;
		radio_reconfigure();

		line_reset();
		line_str("[addr] trigger ");
		line_hex(g_addr, g_addr_len);
		line_str(" (");
		line_u32(g_addr_len);
		line_str(" bytes)\r\n");
		line_flush();
	}

	line_reset();
	line_str("[addr] BALEN=");
	line_u32(g_balen_written);
	line_str(" BASE0=0x");
	for (int shift = 24; shift >= 0; shift -= 8) {
		line_hex_byte((uint8_t)((g_base0_written >> shift) & 0xFFu));
	}
	line_str(" PREFIX0=0x");
	line_hex_byte((uint8_t)(g_prefix0_written & 0xFFu));
	line_str("\r\n");
	line_flush();
}

/* "t[1|2][<hex>]".  A segment selector is present when the argument has an odd
 * number of characters and starts with '1' or '2' (the payload itself is
 * always an even number of hex digits).  So "t12AB" loads segment 1 = 12 AB
 * (even length, no selector), "t112AB" loads the same with an explicit
 * selector, and "t212AB" loads segment 2 = 12 AB. */
static void cmd_tx_load(const char *arg)
{
	size_t n = strlen(arg);
	uint8_t seg = 0;
	uint8_t count = 0;
	static uint8_t bytes[PAYLOAD_MAX];

	if (n == 0u) {
		g_tx_seg_len[0] = 0;
		g_tx_seg_len[1] = 0;
		out_line("[t] both segments cleared");
		return;
	}
	if ((n & 1u) != 0u) {
		if (arg[0] != '1' && arg[0] != '2') {
			out_line("[t] usage: t[1|2]<hex>, even number of hex digits");
			return;
		}
		seg = (uint8_t)(arg[0] - '1');
		arg++;
		n--;
	}
	if (n == 0u) {
		g_tx_seg_len[seg] = 0;
		line_reset();
		line_str("[t] seg");
		line_u32(seg + 1u);
		line_str(" cleared\r\n");
		line_flush();
		return;
	}
	if (n > 2u * PAYLOAD_MAX) {
		out_line("[t] too long: at most 255 bytes (510 hex digits)");
		return;
	}
	while (*arg != '\0') {
		int hi = hex_nibble(arg[0]);
		int lo = hex_nibble(arg[1]);

		if (hi < 0 || lo < 0) {
			out_line("[t] bad hex digit; segment unchanged");
			return;
		}
		bytes[count++] = (uint8_t)((hi << 4) | lo);
		arg += 2;
	}
	memcpy(g_tx_seg[seg], bytes, count);
	g_tx_seg_len[seg] = count;

	line_reset();
	line_str("[t] seg");
	line_u32(seg + 1u);
	line_str(" len=");
	line_u32(count);
	line_ch(' ');
	line_hex(g_tx_seg[seg], count);
	line_str("\r\n");
	line_flush();
}

static void cmd_hop(const char *arg)
{
	uint8_t  chan[32];
	uint8_t  count = 0;
	uint32_t dwell = 200;
	const char *p = arg;

	if (*p == '\0') {
		if (g_hop.active) {
			g_hop.active = false;
			g_channel = g_hop.fixed_channel;
			radio_retune();
			out_line("[hop] stopped, back to the fixed channel");
		} else {
			out_line("[hop] not hopping; usage: y10,30,50,70@200");
		}
		return;
	}

	while (*p != '\0' && *p != '@') {
		uint32_t value = 0;
		bool any = false;

		while (*p >= '0' && *p <= '9') {
			value = value * 10u + (uint32_t)(*p - '0');
			any = true;
			p++;
		}
		if (!any || value > CHANNEL_MAX || count >= 32u) {
			out_line("[hop] bad list; usage: y10,30,50,70@200 (ch 0..100)");
			return;
		}
		chan[count++] = (uint8_t)value;
		if (*p == ',') {
			p++;
		} else if (*p != '\0' && *p != '@') {
			out_line("[hop] bad list; usage: y10,30,50,70@200 (ch 0..100)");
			return;
		}
	}
	if (count == 0u) {
		out_line("[hop] empty list; usage: y10,30,50,70@200");
		return;
	}
	if (*p == '@') {
		if (!parse_u32(p + 1, &dwell) || dwell < 5u || dwell > 10000u) {
			out_line("[hop] dwell must be 5..10000 ms; usage: y10,30@200");
			return;
		}
	}

	memcpy(g_hop.chan, chan, count);
	g_hop.count = count;
	g_hop.dwell_ms = dwell;
	g_hop.idx = 0;
	g_hop.fixed_channel = g_channel;
	g_hop.last_ms = k_uptime_get();
	g_hop.active = true;
	g_channel = chan[0];
	radio_retune();

	line_reset();
	line_str("[hop] ");
	line_u32(count);
	line_str(" channels @ ");
	line_u32(dwell);
	line_str(" ms, starting on ch ");
	line_u32(g_channel);
	line_str("\r\n");
	line_flush();
}

/* "m1" / "m2" / "m250": data rate for RX and TX.  Only MODE changes; address,
 * PCNF0/1, CRC and bit reversal stay as they are.  A running capture is
 * stopped and restarted so the receiver comes up in the new mode. */
static void cmd_set_rate(const char *arg)
{
	uint8_t rate;
	bool was_capturing = g_capturing;

	if (strcmp(arg, "1") == 0) {
		rate = RATE_1M;
	} else if (strcmp(arg, "2") == 0) {
		rate = RATE_2M;
	} else if (strcmp(arg, "b1") == 0) {
		rate = RATE_BLE1M;
	} else if (strcmp(arg, "b2") == 0) {
		rate = RATE_BLE2M;
	} else if (strcmp(arg, "250") == 0) {
		if (!HAVE_RATE_250K) {
			out_line("[cfg] Nrf_250Kbit is not in this nRF52840 MDK "
				 "(deprecated); use m1 or m2, unchanged");
			return;
		}
		rate = RATE_250K;
	} else {
		out_line(HAVE_RATE_250K ? "[cfg] usage: m1 | m2 | mb1 | mb2 | m250"
					: "[cfg] usage: m1 | m2 | mb1 | mb2");
		return;
	}

	if (was_capturing) {
		radio_stop_rx();
	}
	g_rate = rate;
	radio_irq_mask();
	nrf_radio_mode_set(NRF_RADIO, rate_mode());
	radio_irq_unmask();
	if (was_capturing) {
		radio_start_rx();
	}

	line_reset();
	line_str("[cfg] data rate -> ");
	line_str(rate_name());
	line_str(" (");
	line_u32(rate_us_per_byte());
	line_str(" us/byte)\r\n");
	line_flush();
}

/* "k<ms>[b0|b1]": carrier for 1..5000 ms, DTX Center unless b0/b1 is given. */
static void cmd_carrier(const char *arg)
{
	char digits[8];
	size_t n = 0;
	uint32_t ms;
	uint8_t dtx = RADIO_MODECNF0_DTX_Center;

	while (arg[n] >= '0' && arg[n] <= '9' && n < sizeof(digits) - 1u) {
		digits[n] = arg[n];
		n++;
	}
	digits[n] = '\0';

	if (arg[n] == 'b' || arg[n] == 'B') {
		if (arg[n + 1] == '0' && arg[n + 2] == '\0') {
			dtx = RADIO_MODECNF0_DTX_B0;
		} else if (arg[n + 1] == '1' && arg[n + 2] == '\0') {
			dtx = RADIO_MODECNF0_DTX_B1;
		} else {
			n = 0; /* force the usage message */
		}
	} else if (arg[n] != '\0') {
		n = 0;
	}

	if (n == 0u || !parse_u32(digits, &ms) || ms < 1u || ms > 5000u) {
		out_line("[k] usage: k<ms>[b0|b1], ms = 1..5000, e.g. k500 or k500b1");
		return;
	}

	tx_carrier(ms, dtx);
}

static void handle_command(const char *cmd)
{
	char letter = cmd[0];
	const char *arg = cmd + 1;
	uint32_t num = 0;
	bool has_num = parse_u32(arg, &num);

	/* "!boot": reboot into the Nordic open USB DFU bootloader without the
	 * RESET button.  The bootloader stays in DFU mode when GPREGRET holds
	 * BOOTLOADER_DFU_START (0xB1).  Full word only, so a stray '!' is ignored. */
	if (strcmp(cmd, "!boot") == 0) {
		radio_stop_rx();
		k_msleep(50);
		NRF_POWER->GPREGRET = 0xB1;
		NVIC_SystemReset();
	}

	switch (letter) {
	case 'd': {
		static const char *const names[] = {
			"STATE", "SHORTS", "INTEN", "RXADDR", "PCNF0", "PCNF1",
			"BASE0", "PREFIX0", "MODE", "FREQ", "PKTPTR", "CRCCNF",
			"HFCLKSTAT", "MODECNF0",
		};
		uint32_t vals[] = {
			NRF_RADIO->STATE, NRF_RADIO->SHORTS, NRF_RADIO->INTENSET,
			NRF_RADIO->RXADDRESSES, NRF_RADIO->PCNF0, NRF_RADIO->PCNF1,
			NRF_RADIO->BASE0, NRF_RADIO->PREFIX0, NRF_RADIO->MODE,
			NRF_RADIO->FREQUENCY, NRF_RADIO->PACKETPTR, NRF_RADIO->CRCCNF,
			NRF_CLOCK->HFCLKSTAT, NRF_RADIO->MODECNF0,
		};
		for (size_t i = 0; i < ARRAY_SIZE(vals); i++) {
			line_reset();
			line_str("[d] ");
			line_str(names[i]);
			line_str("=0x");
			for (int sh = 24; sh >= 0; sh -= 8) {
				line_hex_byte((uint8_t)(vals[i] >> sh));
			}
			line_str("\r\n");
			line_flush();
		}
		line_reset();
		line_str("[d] isr calls=");
		line_u32(g_isr_calls);
		line_str(" addr=");
		line_u32(g_isr_addr);
		line_str(" end=");
		line_u32(g_isr_end);
		line_str(" disabled=");
		line_u32(g_isr_dis);
		line_str(" irq_enabled=");
		line_u32((uint32_t)irq_is_enabled(RADIO_IRQn));
		line_str("\r\n");
		line_flush();
		line_reset();
		line_str("[d] rate=");
		line_str(rate_name());
		line_str(" us_per_byte=");
		line_u32(rate_us_per_byte());
		line_str("\r\n");
		line_flush();
		line_reset();
		line_str("[d] tx ready=");
		line_u32(g_tx_ready_total);
		line_str(" end=");
		line_u32(g_tx_end_total);
		line_str(" timeouts=");
		line_u32(g_tx_timeout_total);
		line_str(" last_gap_us=");
		if (g_tx_gap_valid) {
			line_i32(g_tx_gap_us);
		} else {
			line_str("n/a");
		}
		line_str("\r\n");
		line_flush();
		break;
	}

	case 'h':
		print_help();
		break;

	case 'p':
		if (g_capturing) {
			radio_stop_rx();
			out_line("[cap] capture OFF");
			if (g_hop.active) {
				g_hop.active = false;
				out_line("[hop] hopping stopped");
			}
		} else {
			radio_start_rx();
			out_line("[cap] capture ON");
			if (g_hop.active) {
				g_hop.idx = 0;
				g_hop.last_ms = k_uptime_get();
			}
		}
		break;

	case 'c':
		if (has_num) {
			cmd_set_channel(num);
		} else {
			out_line("[cfg] usage: c<n>, n = 0..100");
		}
		break;

	case 'l':
		if (has_num) {
			cmd_set_len(num);
		} else {
			out_line("[cfg] usage: l<n>, n = 1..255");
		}
		break;

	case 'w':
		cmd_set_addr(arg);
		break;

	case 'o':
		g_order_rev = !g_order_rev;
		radio_reconfigure();
		line_reset();
		line_str("[addr] byte order -> ");
		line_str(g_order_rev ? "reversed\r\n" : "as given\r\n");
		line_flush();
		break;

	case 'r':
		g_bit_rev = !g_bit_rev;
		radio_reconfigure();
		line_reset();
		line_str("[addr] bit reversal -> ");
		line_str(g_bit_rev ? "on\r\n" : "off\r\n");
		line_flush();
		break;

	case 's':
		energy_scan(has_num ? num : 0u);
		break;

	case 'e':
		energy_time(has_num ? num : 0u);
		break;

	case 'y':
		cmd_hop(arg);
		break;

	case 'x':
		replay_frame(has_num ? num : 0u);
		break;

	case 't':
		cmd_tx_load(arg);
		break;

	case 'z':
		if (*arg != '\0' && !has_num) {
			out_line("[z] usage: z<n>, n = 1..1000 (default 1)");
			break;
		}
		tx_segments(has_num ? num : 0u);
		break;

	case 'm':
		cmd_set_rate(arg);
		break;

	case 'k':
		cmd_carrier(arg);
		break;

	case 'j': {
		/* "j<ms>[b0|b1|c]": carrier lead-in for 'z' (0 = off). */
		uint32_t ms = has_num ? num : 0u;
		const char *p = arg;

		while (*p >= '0' && *p <= '9') {
			p++;
		}
		if (ms > 5000u) {
			ms = 5000u;
		}
		g_tx_lead_us = ms * 1000u;
		if (strcmp(p, "b0") == 0) {
			g_tx_lead_dtx = RADIO_MODECNF0_DTX_B0;
		} else if (strcmp(p, "c") == 0) {
			g_tx_lead_dtx = RADIO_MODECNF0_DTX_Center;
		} else {
			g_tx_lead_dtx = RADIO_MODECNF0_DTX_B1;
		}
		line_reset();
		line_str("[cfg] z lead-in carrier ");
		line_u32(ms);
		line_str(" ms dtx=");
		line_str(g_tx_lead_dtx == RADIO_MODECNF0_DTX_B0 ? "b0" :
			 g_tx_lead_dtx == RADIO_MODECNF0_DTX_Center ? "center" : "b1");
		line_str("\r\n");
		line_flush();
		break;
	}

	case '\0':
		break;

	default:
		line_reset();
		line_str("[?] unknown command '");
		line_ch(letter);
		line_str("' ('h' for help)\r\n");
		line_flush();
		break;
	}
}

/* ------------------------------------------------------------------------- */
/* Main                                                                      */
/* ------------------------------------------------------------------------- */

/* Longest command: "t1" + 510 hex digits = 512 characters.  The CDC ACM RX
 * FIFO is 1 kB (rx-fifo-size default) and the driver stops accepting USB OUT
 * data while it is full, so a long line is not lost between 1 ms polls. */
#define CMD_MAX 600

static void poll_console_input(void)
{
	static char     buf[CMD_MAX + 1];
	static uint16_t buf_len;
	static bool     overflow;
	unsigned char   c;

	while (uart_poll_in(console_dev, &c) == 0) {
		if (c == (unsigned char)'\r' || c == (unsigned char)'\n') {
			if (overflow) {
				out_line("[?] command line too long, ignored");
			} else if (buf_len > 0u) {
				buf[buf_len] = '\0';
				handle_command(buf);
			}
			buf_len = 0;
			overflow = false;
			continue;
		}
		if (buf_len < CMD_MAX) {
			buf[buf_len++] = (char)c;
		} else {
			overflow = true;
		}
	}
}

int main(void)
{
	ring_buf_init(&g_tx_rb, (uint32_t)sizeof(g_tx_area), g_tx_area);

	hfxo_start();
	timer_capture_init();

	IRQ_CONNECT(RADIO_IRQn, 0, radio_isr, NULL, 0);
	irq_enable(RADIO_IRQn);

	/* Configure the radio but leave the receiver off: capture always starts
	 * in the OFF state ('p' enables it). */
	radio_configure();

	g_console_ok = device_is_ready(console_dev);
	if (g_console_ok) {
		out_line("[ok] chilipad-sniffer ready, capture OFF - send 'h' for help");
	}

	while (true) {
		if (!g_console_ok) {
			/* The CDC ACM UART is initialised by the board at boot; retry in
			 * case USB enumeration comes up late. */
			g_console_ok = device_is_ready(console_dev);
		}

		tx_pump();
		if (g_console_ok) {
			poll_console_input();
		}

		int64_t now_ms = k_uptime_get();

		/* Hop mode: advance the channel list. */
		if (g_hop.active && g_capturing &&
		    (now_ms - g_hop.last_ms) >= (int64_t)g_hop.dwell_ms) {
			g_hop.idx = (uint8_t)((g_hop.idx + 1u) % g_hop.count);
			g_channel = g_hop.chan[g_hop.idx];
			g_hop.last_ms = now_ms;
			radio_retune();
		}

		if (g_capturing) {
			(void)drain_packets(16);
			report_drops(now_ms);
		}

		k_msleep(1);
	}

	return 0;
}
