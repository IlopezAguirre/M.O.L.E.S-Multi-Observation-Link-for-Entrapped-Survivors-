/*
 * M.O.L.E.S. — mole firmware (ONE sketch for every mole)
 * Set MOLE_ID below before flashing:  MOLE_A on A, MOLE_B on B, MOLE_C on C.
 *
 * The host ESP32 is the cycle master. It broadcasts a beacon every 100 ms
 * (ESP-NOW). Every mole times its slots from that beacon, so each link
 * succeeds or fails on its own (a blocked A-B beam never takes down A-C or B-C).
 *
 *   slot 0  (+10 ms)  A polls B  -> link 1 "A->B"  (A captures CIR, A sends packet)
 *   slot 1  (+43 ms)  A polls C  -> link 2 "A->C"  (A captures CIR, A sends packet)
 *   slot 2  (+76 ms)  B polls C  -> link 3 "B->C"  (B captures CIR, B sends packet)
 *
 * Each exchange is the same SS-TWR as PUPS:
 *   initiator poll (T1) -> responder stamps T2, replies at T3 with T2+T3 inside
 *   -> initiator stamps T4, computes ToF, reads the CIR window, sends mole_pkt_t to the host.
 *
 * Changes vs PUPS:
 *   - one sketch, role per slot from SCHEDULE[] (edit it to change the layout, e.g. a ring)
 *   - frames carry real source/destination IDs; responders only answer polls addressed
 *     to them from the scheduled initiator; initiators only accept the matching reply
 *   - responders listen ONLY inside their scheduled slot (radio off otherwise)
 *   - seq = beacon cycle counter (low 16 bits): every link shares one timebase
 *   - packet format unchanged (234 bytes)
 *
 * Library: Makerfabs "Dw3000". Board: classic ESP32 DevKit. Arduino-ESP32 core 2.x or 3.x.
 */

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_timer.h>
#include <math.h>
#include "dw3000.h"

// ============================== PER-BOARD CONFIG ==============================
#define MOLE_A 1
#define MOLE_B 2
#define MOLE_C 3
#ifndef MOLE_ID
#define MOLE_ID MOLE_A          // <-- CHANGE PER BOARD: MOLE_A / MOLE_B / MOLE_C
#endif
// ==============================================================================

// ============================== SHARED CONFIG =================================
#define ESPNOW_CHANNEL 1        // must match host_esp32.ino
static uint8_t HOST_MAC[6] = {0x20, 0x50, 0x0D, 0xE4, 0x46, 0x40};

// DWM3000EVB -> ESP32 wiring (same on every mole)
const uint8_t PIN_RST = 27;     // RSTn    — CON4, top pin
const uint8_t PIN_IRQ = 34;     // IRQ     — CON1, 1st from bottom
const uint8_t PIN_SS  = 5;      // SPICSn  — CON1, 3rd from bottom
// SPI defaults: SCK 18, MISO 19, MOSI 23. WAKEUP not connected.

#define ALERT_LED_PIN 2         // onboard LED on most DevKits; wire a red LED here for the exhibit

// Slot schedule: who polls whom in each slot of the 100 ms cycle.
typedef struct { uint8_t initiator, responder, link_id; } slot_t;
static const slot_t SCHEDULE[] = {
  {MOLE_A, MOLE_B, 1},          // slot 0: A->B
  {MOLE_A, MOLE_C, 2},          // slot 1: A->C
  {MOLE_B, MOLE_C, 3},          // slot 2: B->C
};
#define N_SLOTS (sizeof(SCHEDULE) / sizeof(SCHEDULE[0]))

#define CYCLE_US         100000  // one beacon per cycle
#define SLOT0_US          10000  // first slot starts 10 ms after the beacon
#define SLOT_US           33000  // slot spacing
#define POLL_DELAY_US      3000  // initiator waits this long into its slot (responder is already listening)
#define LISTEN_US         15000  // responder's listen window from slot start
#define LATE_LIMIT_US      4000  // a slot we reach later than this is skipped, never run late
#define BEACON_GRACE_US    8000  // how late a beacon may be before we predict the cycle
#define MAX_PREDICT           3  // cycles we may predict without a beacon before declaring sync lost
// ==============================================================================

// ------------------------------- beacon (host -> moles) -----------------------
#define BEACON_MAGIC 0x42
typedef struct __attribute__((packed)) {
  uint8_t  magic;               // BEACON_MAGIC
  uint8_t  version;             // 1
  uint32_t cycle;               // increments every 100 ms
  uint8_t  alert_mask;          // bit (id-1) set = that mole flashes its alert LED
} beacon_t;

// ------------------------------- packet (mole -> host), unchanged -------------
#define CIR_LEN     1016
#define CIR_PRE     8
#define CIR_TAPS    56
#define CIR_SHIFT   2
#define MOLE_MAGIC  0x4D

typedef struct __attribute__((packed)) {
  uint8_t  magic;
  uint8_t  link_id;             // 1 A->B, 2 A->C, 3 B->C
  uint16_t seq;                 // beacon cycle (low 16 bits): shared timebase across links
  float    range_m;             // NAN if the responder didn't answer
  uint16_t fp_idx;              // first-path tap; 0xFFFF if the exchange failed
  int16_t  cir_i[CIR_TAPS];     // window starts at fp_idx - CIR_PRE
  int16_t  cir_q[CIR_TAPS];
} mole_pkt_t;
static_assert(sizeof(mole_pkt_t) == 234, "mole_pkt_t must stay 234 bytes");

// ------------------------------- DW3000 config (identical on every mole) ------
static dwt_config_t config = {
  5, DWT_PLEN_128, DWT_PAC8, 9, 9, 1, DWT_BR_6M8, DWT_PHRMODE_STD, DWT_PHRRATE_STD,
  (129 + 8 - 8), DWT_STS_MODE_OFF, DWT_STS_LEN_64, DWT_PDOA_M0
};
extern dwt_txconfig_t txconfig_options;

#define TX_ANT_DLY 16385
#define RX_ANT_DLY 16385
#define POLL_TX_TO_RESP_RX_DLY_UUS 600   // initiator: RX turns on this long after the poll
#define RESP_RX_TIMEOUT_UUS        400   // initiator: then waits this long for the reply
#define POLL_RX_TO_RESP_TX_DLY_UUS 900   // responder: reply exactly this long after T2

// ------------------------------- addressed 802.15.4 frames --------------------
// [0..1] frame control, [2] seq no, [3..4] PAN, [5..6] dst, [7..8] src, [9] function
#define FC_LO 0x41
#define FC_HI 0x88
#define PAN_LO 0xCA
#define PAN_HI 0xDE
#define ADDR_HI 'M'                       // short address = {id, 'M'}
#define FUNC_POLL 0xE0
#define FUNC_RESP 0xE1
#define IDX_SN 2
#define IDX_DST 5
#define IDX_SRC 7
#define IDX_FUNC 9
#define IDX_T2 10
#define IDX_T3 14
#define POLL_LEN 12                       // 10 header bytes + 2 FCS
#define RESP_LEN 20                       // 18 bytes + 2 FCS

// ------------------------------- stats ----------------------------------------
typedef struct { uint32_t ok, miss, cir_fail, espnow_err; float last_range; } init_stats_t;
typedef struct { uint32_t polls, replies, late, no_poll, wrong, rx_err; } resp_stats_t;
static init_stats_t init_st[N_SLOTS];
static resp_stats_t resp_st[N_SLOTS];
static uint32_t n_late_slots = 0, n_predicted = 0, n_sync_lost = 0;

// ------------------------------- beacon sync (written by the WiFi task) -------
static portMUX_TYPE sync_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool     bc_new = false;
static volatile uint32_t bc_cycle = 0;
static volatile int64_t  bc_time = 0;
static volatile uint8_t  bc_alert = 0;
static volatile uint32_t n_beacons = 0;

// ------------------------------- cycle state (loop only) ----------------------
static bool     synced = false;
static uint32_t cyc = 0;
static int64_t  cyc_start = 0;
static uint8_t  predicted_run = 0;
static bool     slot_done[N_SLOTS];
static uint8_t  alert_mask = 0;
static uint32_t cycles_seen = 0;
static uint8_t  poll_sn = 0;
static uint8_t  rx_buffer[RESP_LEN];

// ==============================================================================
static void halt(const char *msg) {
  Serial.println(msg);
  while (1) delay(1000);
}

static inline char mole_name(uint8_t id) { return (char)('A' + id - 1); }

// ---- ESP-NOW receive: beacons only. Tiny on purpose (runs in the WiFi task). ----
static void handle_espnow(const uint8_t *data, int len) {
  if (len != (int)sizeof(beacon_t)) return;
  beacon_t b;
  memcpy(&b, data, sizeof(b));
  if (b.magic != BEACON_MAGIC) return;
  int64_t now = esp_timer_get_time();
  portENTER_CRITICAL(&sync_mux);
  bc_cycle = b.cycle;
  bc_time = now;
  bc_alert = b.alert_mask;
  bc_new = true;
  n_beacons = n_beacons + 1;
  portEXIT_CRITICAL(&sync_mux);
}

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
static void on_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) { handle_espnow(data, len); }
#else
static void on_recv(const uint8_t *mac, const uint8_t *data, int len) { handle_espnow(data, len); }
#endif

static void espnow_setup() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) halt("ESP-NOW init FAILED");
  esp_now_register_recv_cb(on_recv);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, HOST_MAC, 6);
  peer.channel = ESPNOW_CHANNEL;
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  if (esp_now_add_peer(&peer) != ESP_OK) halt("ESP-NOW add host peer FAILED");
}

static void dw3000_setup() {
  spiBegin(PIN_IRQ, PIN_RST);
  spiSelect(PIN_SS);
  delay(2);
  if (!dwt_checkidlerc()) halt("DW3000 IDLE FAILED (check wiring/power)");
  if (dwt_initialise(DWT_DW_INIT) == DWT_ERROR) halt("DW3000 INIT FAILED");
  dwt_setleds(DWT_LEDS_ENABLE | DWT_LEDS_INIT_BLINK);
  if (dwt_configure(&config)) halt("DW3000 CONFIG FAILED");
  dwt_configuretxrf(&txconfig_options);
  dwt_setrxantennadelay(RX_ANT_DLY);
  dwt_settxantennadelay(TX_ANT_DLY);
  dwt_setlnapamode(DWT_LNA_ENABLE | DWT_PA_ENABLE);
  dwt_configciadiag(DW_CIA_DIAG_LOG_ALL);   // needed for the first-path index
}

// ---- frames ----
static void write_header(uint8_t *f, uint8_t sn, uint8_t dst, uint8_t src, uint8_t func) {
  f[0] = FC_LO; f[1] = FC_HI; f[IDX_SN] = sn; f[3] = PAN_LO; f[4] = PAN_HI;
  f[IDX_DST] = dst; f[IDX_DST + 1] = ADDR_HI;
  f[IDX_SRC] = src; f[IDX_SRC + 1] = ADDR_HI;
  f[IDX_FUNC] = func;
}

static bool header_is(const uint8_t *f, uint8_t dst, uint8_t src, uint8_t func) {
  return f[0] == FC_LO && f[1] == FC_HI && f[3] == PAN_LO && f[4] == PAN_HI
      && f[IDX_DST] == dst && f[IDX_DST + 1] == ADDR_HI
      && f[IDX_SRC] == src && f[IDX_SRC + 1] == ADDR_HI
      && f[IDX_FUNC] == func;
}

static void clear_status() {
  dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_RXFCG_BIT_MASK | SYS_STATUS_TXFRS_BIT_MASK |
                                   SYS_STATUS_ALL_RX_TO | SYS_STATUS_ALL_RX_ERR);
}

// ---- CIR window (same as PUPS) ----
static inline int32_t acc18(const uint8_t *b) {
  int32_t v = (int32_t)b[0] | ((int32_t)b[1] << 8) | ((int32_t)(b[2] & 0x03) << 16);
  if (v & 0x20000) v -= 0x40000;
  return v;
}

static bool read_cir_window(mole_pkt_t *p) {
  dwt_rxdiag_t diag;
  dwt_readdiagnostics(&diag);
  uint16_t fp = diag.ipatovFpIndex >> 6;        // 10.6 fixed point -> integer tap
  if (fp == 0 || fp >= CIR_LEN) return false;
  int start = (int)fp - CIR_PRE;
  if (start < 0) start = 0;
  if (start + CIR_TAPS > CIR_LEN) start = CIR_LEN - CIR_TAPS;
  static uint8_t acc[CIR_TAPS * 6 + 1];         // +1: first byte is a dummy
  dwt_readaccdata(acc, sizeof(acc), (uint16_t)start);   // offset is a sample index
  for (int i = 0; i < CIR_TAPS; i++) {
    const uint8_t *s = &acc[1 + i * 6];
    p->cir_i[i] = (int16_t)(acc18(&s[0]) >> CIR_SHIFT);
    p->cir_q[i] = (int16_t)(acc18(&s[3]) >> CIR_SHIFT);
  }
  p->fp_idx = (uint16_t)(start + CIR_PRE);
  return true;
}

// ---- INITIATOR: poll `target`, compute ToF, read CIR. Fills pkt on success. ----
static void ranging_exchange(uint8_t target, mole_pkt_t *pkt, init_stats_t *st) {
  dwt_forcetrxoff();
  clear_status();
  dwt_setrxaftertxdelay(POLL_TX_TO_RESP_RX_DLY_UUS);
  dwt_setrxtimeout(RESP_RX_TIMEOUT_UUS);

  uint8_t sn = poll_sn++;
  uint8_t poll[POLL_LEN] = {0};
  write_header(poll, sn, target, MOLE_ID, FUNC_POLL);
  dwt_writetxdata(sizeof(poll), poll, 0);
  dwt_writetxfctrl(sizeof(poll), 0, 1);
  dwt_starttx(DWT_START_TX_IMMEDIATE | DWT_RESPONSE_EXPECTED);           // T1 in hardware

  uint32_t status;
  int64_t t0 = esp_timer_get_time();
  while (!((status = dwt_read32bitreg(SYS_STATUS_ID)) &
           (SYS_STATUS_RXFCG_BIT_MASK | SYS_STATUS_ALL_RX_TO | SYS_STATUS_ALL_RX_ERR))) {
    if (esp_timer_get_time() - t0 > 20000) { dwt_forcetrxoff(); break; }   // safety net
  }

  if (!(status & SYS_STATUS_RXFCG_BIT_MASK)) { clear_status(); st->miss++; return; }
  clear_status();

  uint32_t len = dwt_read32bitreg(RX_FINFO_ID) & RXFLEN_MASK;
  if (len != RESP_LEN) { st->miss++; return; }
  dwt_readrxdata(rx_buffer, len, 0);
  if (!header_is(rx_buffer, MOLE_ID, target, FUNC_RESP) || rx_buffer[IDX_SN] != sn) { st->miss++; return; }

  uint32_t poll_tx_ts = dwt_readtxtimestamplo32();                        // T1
  uint32_t resp_rx_ts = dwt_readrxtimestamplo32();                        // T4
  uint32_t poll_rx_ts, resp_tx_ts;
  resp_msg_get_ts(&rx_buffer[IDX_T2], &poll_rx_ts);                       // T2 (from responder)
  resp_msg_get_ts(&rx_buffer[IDX_T3], &resp_tx_ts);                       // T3 (from responder)

  float clockOffsetRatio = ((float)dwt_readclockoffset()) / (uint32_t)(1 << 26);
  int32_t rtd_init = resp_rx_ts - poll_tx_ts;
  int32_t rtd_resp = resp_tx_ts - poll_rx_ts;
  double tof = ((rtd_init - rtd_resp * (1 - clockOffsetRatio)) / 2.0) * DWT_TIME_UNITS;

  pkt->range_m = (float)(tof * SPEED_OF_LIGHT);
  st->last_range = pkt->range_m;
  st->ok++;
  if (!read_cir_window(pkt)) st->cir_fail++;
}

static void run_initiator(size_t k, int64_t slot_start) {
  const slot_t &sl = SCHEDULE[k];
  while (esp_timer_get_time() < slot_start + POLL_DELAY_US) { }          // responder is listening

  mole_pkt_t pkt;
  memset(&pkt, 0, sizeof(pkt));
  pkt.magic = MOLE_MAGIC;
  pkt.link_id = sl.link_id;
  pkt.seq = (uint16_t)cyc;
  pkt.range_m = NAN;
  pkt.fp_idx = 0xFFFF;

  ranging_exchange(sl.responder, &pkt, &init_st[k]);
  if (esp_now_send(HOST_MAC, (const uint8_t *)&pkt, sizeof(pkt)) != ESP_OK) init_st[k].espnow_err++;
}

// ---- RESPONDER: listen only in this slot, answer only the scheduled initiator. ----
static void run_responder(size_t k, int64_t slot_start) {
  const slot_t &sl = SCHEDULE[k];
  resp_stats_t *st = &resp_st[k];
  int64_t end = slot_start + LISTEN_US;

  dwt_forcetrxoff();
  clear_status();
  dwt_setrxtimeout(0);                                                    // software window instead
  dwt_rxenable(DWT_START_RX_IMMEDIATE);

  while (true) {
    uint32_t status = dwt_read32bitreg(SYS_STATUS_ID);

    if (status & SYS_STATUS_RXFCG_BIT_MASK) {
      dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_RXFCG_BIT_MASK);
      uint32_t len = dwt_read32bitreg(RX_FINFO_ID) & RXFLEN_MASK;
      uint8_t f[POLL_LEN];
      if (len == POLL_LEN) {
        dwt_readrxdata(f, len, 0);
        if (header_is(f, MOLE_ID, sl.initiator, FUNC_POLL)) {
          st->polls++;
          uint64_t poll_rx_ts = get_rx_timestamp_u64();                   // T2
          uint32_t resp_tx_time = (poll_rx_ts + (POLL_RX_TO_RESP_TX_DLY_UUS * UUS_TO_DWT_TIME)) >> 8;
          dwt_setdelayedtrxtime(resp_tx_time);
          uint64_t resp_tx_ts = (((uint64_t)(resp_tx_time & 0xFFFFFFFEUL)) << 8) + TX_ANT_DLY;   // T3

          uint8_t resp[RESP_LEN] = {0};
          write_header(resp, f[IDX_SN], sl.initiator, MOLE_ID, FUNC_RESP);   // echo the poll's SN
          resp_msg_set_ts(&resp[IDX_T2], poll_rx_ts);
          resp_msg_set_ts(&resp[IDX_T3], resp_tx_ts);
          dwt_writetxdata(sizeof(resp), resp, 0);
          dwt_writetxfctrl(sizeof(resp), 0, 1);

          if (dwt_starttx(DWT_START_TX_DELAYED) == DWT_SUCCESS) {
            int64_t t0 = esp_timer_get_time();
            while (!(dwt_read32bitreg(SYS_STATUS_ID) & SYS_STATUS_TXFRS_BIT_MASK)) {
              if (esp_timer_get_time() - t0 > 5000) break;
            }
            dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS_BIT_MASK);
            st->replies++;
          } else {
            st->late++;                                                   // missed the 900 us deadline
          }
          return;
        }
      }
      st->wrong++;                                                        // not for us: keep listening
      dwt_rxenable(DWT_START_RX_IMMEDIATE);
    } else if (status & (SYS_STATUS_ALL_RX_ERR | SYS_STATUS_ALL_RX_TO)) {
      dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_ALL_RX_ERR | SYS_STATUS_ALL_RX_TO);
      st->rx_err++;
      dwt_rxenable(DWT_START_RX_IMMEDIATE);
    }

    if (esp_timer_get_time() > end) {
      dwt_forcetrxoff();
      clear_status();
      st->no_poll++;
      return;
    }
  }
}

// ---- cycle timing: follow the beacon; predict up to MAX_PREDICT cycles if one is missed ----
static bool update_sync(int64_t now) {                                     // true = a new cycle began
  bool nb;
  uint32_t c;
  int64_t t;
  uint8_t a;
  portENTER_CRITICAL(&sync_mux);
  nb = bc_new; c = bc_cycle; t = bc_time; a = bc_alert;
  bc_new = false;
  portEXIT_CRITICAL(&sync_mux);

  if (nb) {
    alert_mask = a;
    bool fresh = !synced || c != cyc;
    cyc = c;
    cyc_start = t;
    synced = true;
    predicted_run = 0;
    return fresh;
  }
  if (synced && now > cyc_start + CYCLE_US + BEACON_GRACE_US) {
    if (predicted_run >= MAX_PREDICT) {
      synced = false;
      n_sync_lost++;
      return false;
    }
    cyc++;
    cyc_start += CYCLE_US;
    predicted_run++;
    n_predicted++;
    return true;
  }
  return false;
}

// ---- runs in the quiet gap after this cycle's last slot ----
static void idle_tasks() {
  static uint32_t last_led_cycle = 0xFFFFFFFF;
  if (last_led_cycle != cyc) {                                            // alert LED: blink at 5 Hz
    last_led_cycle = cyc;
    bool alerted = (alert_mask >> (MOLE_ID - 1)) & 1;
    digitalWrite(ALERT_LED_PIN, alerted && (cyc & 1) ? HIGH : LOW);
  }

  static uint32_t last_print = 0;
  if (cycles_seen - last_print < 20) return;                             // every ~2 s
  last_print = cycles_seen;
  Serial.printf("[%c] cyc %lu %s beacons %lu pred %lu lost %lu late-slots %lu |",
                mole_name(MOLE_ID), (unsigned long)cyc, predicted_run ? "PRED" : "sync",
                (unsigned long)n_beacons, (unsigned long)n_predicted, (unsigned long)n_sync_lost,
                (unsigned long)n_late_slots);
  for (size_t k = 0; k < N_SLOTS; k++) {
    const slot_t &sl = SCHEDULE[k];
    if (sl.initiator == MOLE_ID)
      Serial.printf(" %c->%c ok %lu miss %lu (%.3f m) |", mole_name(sl.initiator), mole_name(sl.responder),
                    (unsigned long)init_st[k].ok, (unsigned long)init_st[k].miss, init_st[k].last_range);
    else if (sl.responder == MOLE_ID)
      Serial.printf(" answer %c: replies %lu late %lu no-poll %lu wrong %lu |", mole_name(sl.initiator),
                    (unsigned long)resp_st[k].replies, (unsigned long)resp_st[k].late,
                    (unsigned long)resp_st[k].no_poll, (unsigned long)resp_st[k].wrong);
  }
  Serial.println();
}

// ==============================================================================
void setup() {
  Serial.setTxBufferSize(1024);            // prints never block the radio timing
  Serial.begin(115200);
  delay(200);
  pinMode(ALERT_LED_PIN, OUTPUT);
  digitalWrite(ALERT_LED_PIN, LOW);

  Serial.printf("\n[%c] M.O.L.E.S. mole %c — schedule:\n", mole_name(MOLE_ID), mole_name(MOLE_ID));
  for (size_t k = 0; k < N_SLOTS; k++) {
    const slot_t &sl = SCHEDULE[k];
    const char *role = sl.initiator == MOLE_ID ? "POLL" : sl.responder == MOLE_ID ? "ANSWER" : "idle";
    Serial.printf("    slot %u (+%lu ms)  %c->%c  link %u   me: %s\n", (unsigned)k,
                  (unsigned long)((SLOT0_US + k * SLOT_US) / 1000), mole_name(sl.initiator),
                  mole_name(sl.responder), sl.link_id, role);
  }

  espnow_setup();
  dw3000_setup();
  Serial.printf("[%c] ready, waiting for host beacon...\n", mole_name(MOLE_ID));
}

void loop() {
  int64_t now = esp_timer_get_time();
  if (update_sync(now)) {
    memset(slot_done, 0, sizeof(slot_done));
    cycles_seen++;
  }
  if (!synced) {
    static int64_t last_warn = 0;
    if (now - last_warn > 2000000) {
      last_warn = now;
      Serial.printf("[%c] no beacon — is the host running on channel %d?\n", mole_name(MOLE_ID), ESPNOW_CHANNEL);
    }
    return;
  }

  for (size_t k = 0; k < N_SLOTS; k++) {
    if (slot_done[k]) continue;
    int64_t s0 = cyc_start + SLOT0_US + (int64_t)k * SLOT_US;
    if (now < s0) break;                                                  // slots are in time order
    slot_done[k] = true;
    const slot_t &sl = SCHEDULE[k];
    if (sl.initiator != MOLE_ID && sl.responder != MOLE_ID) continue;    // idle slot for this mole
    if (now > s0 + LATE_LIMIT_US) { n_late_slots++; continue; }           // never run a slot late
    if (sl.initiator == MOLE_ID) run_initiator(k, s0);
    else if (sl.responder == MOLE_ID) run_responder(k, s0);
    now = esp_timer_get_time();
  }

  if (slot_done[N_SLOTS - 1]) idle_tasks();
}
