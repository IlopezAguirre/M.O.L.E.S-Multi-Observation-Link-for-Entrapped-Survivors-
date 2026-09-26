/*
 * MOLES — Phase 1: Hand-Wave Test
 * MOLE A  (TX / SS-TWR INITIATOR)  — flash this onto the ESP32 wired to DWM3000EVB "A"
 *
 * Every 100 ms (10 Hz):
 *   1. Send a UWB poll to Mole B                          (T1 = poll TX timestamp)
 *   2. Receive Mole B's response containing T2 and T3     (T4 = response RX timestamp)
 *   3. ToF = ((T4 - T1) - (T3 - T2)) / 2   ->  range_m
 *   4. Read the first-path index + a 56-tap CIR window around it
 *   5. Send one mole_pkt_t (234 bytes) to the host ESP32 over ESP-NOW
 *   If Mole B doesn't answer, a packet is still sent with range_m = NAN and fp_idx = 0xFFFF
 *   so the terminal can show the dropout (a fully blocked link is itself a detection).
 *
 * Library: Makerfabs "Dw3000" (github.com/Makerfabs/Makerfabs-ESP32-UWB-DW3000, folder Dw3000/)
 * Board:   classic ESP32 DevKit (ESP32-WROOM-32). Arduino-ESP32 core 2.x or 3.x.
 */

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <math.h>
#include "dw3000.h"

// ============================== USER CONFIG ==============================
#define LINK_ID           0x01   // A->B. Give each link its own ID when mole C joins.
#define ESPNOW_CHANNEL    1      // Must match host_esp32.ino
#define RANGE_PERIOD_MS   100    // 10 Hz

// Host MAC: broadcast works out of the box. For fewer dropped packets, paste the MAC
// the monitor prints on startup ("[host] mac=...") here — unicast gets ACK + retries.
static uint8_t HOST_MAC[6] = {0x20, 0x50, 0x0D, 0xE4, 0x46, 0x40};

// DWM3000EVB -> ESP32 DevKit wiring (see HAND_WAVE_TEST.md)
const uint8_t PIN_RST = 27;   // RSTn    — CON4, top pin
const uint8_t PIN_IRQ = 34;   // IRQ     — CON1, 1st from bottom
const uint8_t PIN_SS  = 5;    // SPICSn  — CON1, 3rd from bottom
// SPI uses the ESP32 defaults: SCK 18 (CON1 6th), MISO 19 (CON1 5th), MOSI 23 (CON1 4th)
// WAKEUP (CON1 2nd) is left unconnected — the firmware never puts the DW3000 to sleep
// ========================================================================

// ----------------------------- CIR window -------------------------------
#define CIR_LEN     1016   // Ipatov CIR length at 64 MHz PRF
#define CIR_PRE     8      // taps kept before the first path
#define CIR_TAPS    56     // total taps sent (8 before + first path + 47 after)
#define CIR_SHIFT   2      // DW3000 taps are 18-bit signed; >>2 fits int16 without clipping

// ------------------------------ packet ----------------------------------
#define MOLE_MAGIC  0x4D   // 'M' — filters out other ESP-NOW traffic at the hackathon

typedef struct __attribute__((packed)) {
  uint8_t  magic;              // MOLE_MAGIC
  uint8_t  link_id;            // which mole pair
  uint16_t seq;                // increments every poll attempt (gaps = lost ESP-NOW packets)
  float    range_m;            // NAN if the exchange failed
  uint16_t fp_idx;             // integer first-path tap index; 0xFFFF if exchange failed
  int16_t  cir_i[CIR_TAPS];    // real part, window starts at fp_idx - CIR_PRE
  int16_t  cir_q[CIR_TAPS];    // imaginary part
} mole_pkt_t;
static_assert(sizeof(mole_pkt_t) == 234, "mole_pkt_t must be 234 bytes (fits one 250-byte ESP-NOW frame)");

// -------------------------- DW3000 settings -----------------------------
// Same default config as the library examples (channel 5, 6.8 Mbps, preamble code 9, no STS).
static dwt_config_t config = {
  5,                /* Channel number */
  DWT_PLEN_128,     /* Preamble length (TX) */
  DWT_PAC8,         /* Preamble acquisition chunk (RX) */
  9,                /* TX preamble code */
  9,                /* RX preamble code */
  1,                /* Non-standard 8-symbol SFD */
  DWT_BR_6M8,       /* Data rate */
  DWT_PHRMODE_STD,  /* PHY header mode */
  DWT_PHRRATE_STD,  /* PHY header rate */
  (129 + 8 - 8),    /* SFD timeout */
  DWT_STS_MODE_OFF, /* STS off */
  DWT_STS_LEN_64,
  DWT_PDOA_M0       /* PDOA off */
};
extern dwt_txconfig_t txconfig_options;

#define TX_ANT_DLY 16385   // uncalibrated default -> absolute range has an offset; deltas are what matter here
#define RX_ANT_DLY 16385

// Timing tuned by Makerfabs for ESP32 (paired with 900 uus in mole_b_rx.ino)
#define POLL_TX_TO_RESP_RX_DLY_UUS 600
#define RESP_RX_TIMEOUT_UUS        400

// Frames (must match mole_b_rx.ino)
static uint8_t tx_poll_msg[] = {0x41, 0x88, 0, 0xCA, 0xDE, 'W', 'A', 'V', 'E', 0xE0, 0, 0};
static uint8_t rx_resp_msg[] = {0x41, 0x88, 0, 0xCA, 0xDE, 'V', 'E', 'W', 'A', 0xE1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
#define ALL_MSG_COMMON_LEN       10
#define ALL_MSG_SN_IDX           2
#define RESP_MSG_POLL_RX_TS_IDX  10
#define RESP_MSG_RESP_TX_TS_IDX  14

#define RX_BUF_LEN 20
static uint8_t rx_buffer[RX_BUF_LEN];
static uint8_t frame_seq_nb = 0;

// ------------------------------ state -----------------------------------
static uint16_t pkt_seq = 0;
static uint32_t n_ok = 0, n_miss = 0, n_cir_fail = 0, n_espnow_err = 0;
static float    last_range = NAN;

// ========================================================================
static void halt(const char *msg) {
  Serial.println(msg);
  while (1) delay(1000);
}

static void espnow_setup() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK) halt("[A] ESP-NOW init FAILED");

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, HOST_MAC, 6);
  peer.channel = ESPNOW_CHANNEL;
  peer.ifidx   = WIFI_IF_STA;
  peer.encrypt = false;
  if (esp_now_add_peer(&peer) != ESP_OK) halt("[A] ESP-NOW add peer FAILED");
}

static void dw3000_setup() {
  spiBegin(PIN_IRQ, PIN_RST);
  spiSelect(PIN_SS);
  delay(2);

  if (!dwt_checkidlerc())                   halt("[A] DW3000 IDLE FAILED (check wiring/power)");
  if (dwt_initialise(DWT_DW_INIT) == DWT_ERROR) halt("[A] DW3000 INIT FAILED");
  dwt_setleds(DWT_LEDS_ENABLE | DWT_LEDS_INIT_BLINK);
  if (dwt_configure(&config))               halt("[A] DW3000 CONFIG FAILED");

  dwt_configuretxrf(&txconfig_options);
  dwt_setrxantennadelay(RX_ANT_DLY);
  dwt_settxantennadelay(TX_ANT_DLY);
  dwt_setrxaftertxdelay(POLL_TX_TO_RESP_RX_DLY_UUS);
  dwt_setrxtimeout(RESP_RX_TIMEOUT_UUS);
  dwt_setlnapamode(DWT_LNA_ENABLE | DWT_PA_ENABLE);

  // Required so dwt_readdiagnostics() fills in the first-path index
  dwt_configciadiag(DW_CIA_DIAG_LOG_ALL);
}

// 18-bit signed accumulator value -> int32
static inline int32_t acc18(const uint8_t *b) {
  int32_t v = (int32_t)b[0] | ((int32_t)b[1] << 8) | ((int32_t)(b[2] & 0x03) << 16);
  if (v & 0x20000) v -= 0x40000;
  return v;
}

// Reads first-path index + CIR window into the packet. Must run before the next RX.
static bool read_cir_window(mole_pkt_t *p) {
  dwt_rxdiag_t diag;
  dwt_readdiagnostics(&diag);
  uint16_t fp = diag.ipatovFpIndex >> 6;   // 10.6 fixed point -> integer tap
  if (fp == 0 || fp >= CIR_LEN) return false;

  int start = (int)fp - CIR_PRE;
  if (start < 0) start = 0;
  if (start + CIR_TAPS > CIR_LEN) start = CIR_LEN - CIR_TAPS;

  static uint8_t acc[CIR_TAPS * 6 + 1];      // +1: first byte is a dummy
  dwt_readaccdata(acc, sizeof(acc), (uint16_t)start);   // offset is a sample index

  for (int i = 0; i < CIR_TAPS; i++) {
    const uint8_t *s = &acc[1 + i * 6];
    p->cir_i[i] = (int16_t)(acc18(&s[0]) >> CIR_SHIFT);
    p->cir_q[i] = (int16_t)(acc18(&s[3]) >> CIR_SHIFT);
  }
  p->fp_idx = (uint16_t)(start + CIR_PRE);    // keep "window start = fp_idx - 8" true even if clamped
  return true;
}

// One SS-TWR exchange. Fills pkt on success; leaves NAN/0xFFFF on failure.
static void ranging_exchange(mole_pkt_t *pkt) {
  tx_poll_msg[ALL_MSG_SN_IDX] = frame_seq_nb;
  dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS_BIT_MASK);
  dwt_writetxdata(sizeof(tx_poll_msg), tx_poll_msg, 0);
  dwt_writetxfctrl(sizeof(tx_poll_msg), 0, 1);
  dwt_starttx(DWT_START_TX_IMMEDIATE | DWT_RESPONSE_EXPECTED);   // T1 captured by hardware

  uint32_t status;
  uint32_t t0 = micros();
  while (!((status = dwt_read32bitreg(SYS_STATUS_ID)) &
           (SYS_STATUS_RXFCG_BIT_MASK | SYS_STATUS_ALL_RX_TO | SYS_STATUS_ALL_RX_ERR))) {
    if (micros() - t0 > 20000) {           // safety net, should never trigger
      dwt_forcetrxoff();
      break;
    }
  }
  frame_seq_nb++;

  if (!(status & SYS_STATUS_RXFCG_BIT_MASK)) {
    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_ALL_RX_TO | SYS_STATUS_ALL_RX_ERR);
    n_miss++;
    return;
  }

  dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_RXFCG_BIT_MASK);
  uint32_t frame_len = dwt_read32bitreg(RX_FINFO_ID) & RXFLEN_MASK;
  if (frame_len > sizeof(rx_buffer)) { n_miss++; return; }

  dwt_readrxdata(rx_buffer, frame_len, 0);
  rx_buffer[ALL_MSG_SN_IDX] = 0;
  if (memcmp(rx_buffer, rx_resp_msg, ALL_MSG_COMMON_LEN) != 0) { n_miss++; return; }

  // T1, T4 from our DW3000; T2, T3 from inside Mole B's response
  uint32_t poll_tx_ts = dwt_readtxtimestamplo32();   // T1
  uint32_t resp_rx_ts = dwt_readrxtimestamplo32();   // T4
  uint32_t poll_rx_ts, resp_tx_ts;
  resp_msg_get_ts(&rx_buffer[RESP_MSG_POLL_RX_TS_IDX], &poll_rx_ts);   // T2
  resp_msg_get_ts(&rx_buffer[RESP_MSG_RESP_TX_TS_IDX], &resp_tx_ts);   // T3

  float clockOffsetRatio = ((float)dwt_readclockoffset()) / (uint32_t)(1 << 26);
  int32_t rtd_init = resp_rx_ts - poll_tx_ts;   // T4 - T1
  int32_t rtd_resp = resp_tx_ts - poll_rx_ts;   // T3 - T2
  double tof = ((rtd_init - rtd_resp * (1 - clockOffsetRatio)) / 2.0) * DWT_TIME_UNITS;

  pkt->range_m = (float)(tof * SPEED_OF_LIGHT);
  last_range   = pkt->range_m;
  n_ok++;

  if (!read_cir_window(pkt)) n_cir_fail++;
}

// ========================================================================
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[A] MOLES phase 1 — Mole A (TX / initiator)");

  espnow_setup();
  dw3000_setup();

  Serial.printf("[A] ready. link_id=0x%02X  ch=%d  host=%02X:%02X:%02X:%02X:%02X:%02X\n",
                LINK_ID, ESPNOW_CHANNEL, HOST_MAC[0], HOST_MAC[1], HOST_MAC[2],
                HOST_MAC[3], HOST_MAC[4], HOST_MAC[5]);
}

void loop() {
  static uint32_t next_ms = 0, next_print = 0;
  uint32_t now = millis();

  if ((int32_t)(now - next_ms) >= 0) {
    next_ms = now + RANGE_PERIOD_MS;

    mole_pkt_t pkt;
    memset(&pkt, 0, sizeof(pkt));
    pkt.magic   = MOLE_MAGIC;
    pkt.link_id = LINK_ID;
    pkt.seq     = pkt_seq++;
    pkt.range_m = NAN;
    pkt.fp_idx  = 0xFFFF;

    ranging_exchange(&pkt);

    if (esp_now_send(HOST_MAC, (const uint8_t *)&pkt, sizeof(pkt)) != ESP_OK) n_espnow_err++;
  }

  if ((int32_t)(now - next_print) >= 0) {   // local debug, only visible if A is on USB
    next_print = now + 2000;
    Serial.printf("[A] ok=%lu miss=%lu cir_fail=%lu espnow_err=%lu last_range=%.3f m\n",
                  (unsigned long)n_ok, (unsigned long)n_miss, (unsigned long)n_cir_fail,
                  (unsigned long)n_espnow_err, last_range);
  }
}
