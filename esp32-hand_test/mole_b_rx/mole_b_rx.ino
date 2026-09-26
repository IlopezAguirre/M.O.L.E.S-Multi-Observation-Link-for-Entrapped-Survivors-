/*
 * MOLES — Phase 1: Hand-Wave Test
 * MOLE B  (RX / SS-TWR RESPONDER)  — flash this onto the ESP32 wired to DWM3000EVB "B"
 *
 * Loop forever:
 *   1. Listen for Mole A's poll                     (T2 = poll RX timestamp)
 *   2. Schedule the response exactly 900 uus later  (T3 = response TX time, known in advance)
 *   3. Pack T2 and T3 into the response and send it over UWB
 * Mole B never talks to the host — no WiFi / ESP-NOW here.
 *
 * Library: Makerfabs "Dw3000" (github.com/Makerfabs/Makerfabs-ESP32-UWB-DW3000, folder Dw3000/)
 * Board:   classic ESP32 DevKit (ESP32-WROOM-32). Arduino-ESP32 core 2.x or 3.x.
 */

#include "dw3000.h"

// ============================== USER CONFIG ==============================
const uint8_t PIN_RST = 27;   // RSTn    — CON4, top pin
const uint8_t PIN_IRQ = 34;   // IRQ     — CON1, 1st from bottom
const uint8_t PIN_SS  = 5;    // SPICSn  — CON1, 3rd from bottom
// SPI uses the ESP32 defaults: SCK 18 (CON1 6th), MISO 19 (CON1 5th), MOSI 23 (CON1 4th)
// WAKEUP (CON1 2nd) is left unconnected — the firmware never puts the DW3000 to sleep
// ========================================================================

// Must match mole_a_tx.ino exactly
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

#define TX_ANT_DLY 16385
#define RX_ANT_DLY 16385

// Poll RX -> response TX delay. Makerfabs' ESP32-tuned value; if "late" climbs in the
// status line, raise this AND raise POLL_TX_TO_RESP_RX_DLY_UUS in mole_a_tx.ino by the same amount.
#define POLL_RX_TO_RESP_TX_DLY_UUS 900

static uint8_t rx_poll_msg[] = {0x41, 0x88, 0, 0xCA, 0xDE, 'W', 'A', 'V', 'E', 0xE0, 0, 0};
static uint8_t tx_resp_msg[] = {0x41, 0x88, 0, 0xCA, 0xDE, 'V', 'E', 'W', 'A', 0xE1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
#define ALL_MSG_COMMON_LEN       10
#define ALL_MSG_SN_IDX           2
#define RESP_MSG_POLL_RX_TS_IDX  10
#define RESP_MSG_RESP_TX_TS_IDX  14

#define RX_BUF_LEN 12
static uint8_t rx_buffer[RX_BUF_LEN];
static uint8_t frame_seq_nb = 0;

static uint32_t n_poll = 0, n_resp = 0, n_late = 0, n_rx_err = 0, n_other = 0;

static void halt(const char *msg) {
  Serial.println(msg);
  while (1) delay(1000);
}

static void print_status_if_due() {
  static uint32_t next_print = 0;
  uint32_t now = millis();
  if ((int32_t)(now - next_print) < 0) return;
  next_print = now + 2000;
  Serial.printf("[B] polls=%lu responses=%lu late=%lu rx_err=%lu other=%lu\n",
                (unsigned long)n_poll, (unsigned long)n_resp, (unsigned long)n_late,
                (unsigned long)n_rx_err, (unsigned long)n_other);
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[B] MOLES phase 1 — Mole B (RX / responder)");

  spiBegin(PIN_IRQ, PIN_RST);
  spiSelect(PIN_SS);
  delay(2);

  if (!dwt_checkidlerc())                   halt("[B] DW3000 IDLE FAILED (check wiring/power)");
  if (dwt_initialise(DWT_DW_INIT) == DWT_ERROR) halt("[B] DW3000 INIT FAILED");
  dwt_setleds(DWT_LEDS_ENABLE | DWT_LEDS_INIT_BLINK);
  if (dwt_configure(&config))               halt("[B] DW3000 CONFIG FAILED");

  dwt_configuretxrf(&txconfig_options);
  dwt_setrxantennadelay(RX_ANT_DLY);
  dwt_settxantennadelay(TX_ANT_DLY);
  dwt_setlnapamode(DWT_LNA_ENABLE | DWT_PA_ENABLE);

  Serial.println("[B] ready, listening for polls");
}

void loop() {
  dwt_rxenable(DWT_START_RX_IMMEDIATE);

  uint32_t status;
  while (!((status = dwt_read32bitreg(SYS_STATUS_ID)) &
           (SYS_STATUS_RXFCG_BIT_MASK | SYS_STATUS_ALL_RX_ERR))) {
    print_status_if_due();   // radio keeps listening while we print
  }

  if (!(status & SYS_STATUS_RXFCG_BIT_MASK)) {
    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_ALL_RX_ERR);
    n_rx_err++;
    return;
  }

  dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_RXFCG_BIT_MASK);
  uint32_t frame_len = dwt_read32bitreg(RX_FINFO_ID) & RXFLEN_MASK;
  if (frame_len > sizeof(rx_buffer)) { n_other++; return; }

  dwt_readrxdata(rx_buffer, frame_len, 0);
  rx_buffer[ALL_MSG_SN_IDX] = 0;
  if (memcmp(rx_buffer, rx_poll_msg, ALL_MSG_COMMON_LEN) != 0) { n_other++; return; }
  n_poll++;

  // T2: when the poll arrived (hardware timestamp)
  uint64_t poll_rx_ts = get_rx_timestamp_u64();

  // T3: schedule the response in the future so we know its TX timestamp before sending
  uint32_t resp_tx_time = (poll_rx_ts + (POLL_RX_TO_RESP_TX_DLY_UUS * UUS_TO_DWT_TIME)) >> 8;
  dwt_setdelayedtrxtime(resp_tx_time);
  uint64_t resp_tx_ts = (((uint64_t)(resp_tx_time & 0xFFFFFFFEUL)) << 8) + TX_ANT_DLY;

  resp_msg_set_ts(&tx_resp_msg[RESP_MSG_POLL_RX_TS_IDX], poll_rx_ts);   // T2
  resp_msg_set_ts(&tx_resp_msg[RESP_MSG_RESP_TX_TS_IDX], resp_tx_ts);   // T3
  tx_resp_msg[ALL_MSG_SN_IDX] = frame_seq_nb;

  dwt_writetxdata(sizeof(tx_resp_msg), tx_resp_msg, 0);
  dwt_writetxfctrl(sizeof(tx_resp_msg), 0, 1);

  if (dwt_starttx(DWT_START_TX_DELAYED) == DWT_SUCCESS) {
    while (!(dwt_read32bitreg(SYS_STATUS_ID) & SYS_STATUS_TXFRS_BIT_MASK)) { }
    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS_BIT_MASK);
    frame_seq_nb++;
    n_resp++;
  } else {
    n_late++;   // ESP32 took longer than 900 uus to prepare the reply
  }
}
