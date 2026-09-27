/*
 * M.O.L.E.S. — HOST ESP32 (cycle master + forwarder). Flash onto the ESP32 plugged into the laptop.
 *
 * 1. Cycle master: broadcasts a beacon every 100 ms over ESP-NOW with a cycle counter.
 *    Every mole times its slots from this beacon (see SCHEDULE[] in mole.ino).
 * 2. Forwarder: receives mole_pkt_t from the moles and forwards it to the laptop over
 *    USB serial (921600 baud) in the same framed format as PUPS.
 * 3. Alerts: a line "ALERT <mask>" from the laptop sets which moles flash their LED
 *    (bit 0 = A, bit 1 = B, bit 2 = C). "ALERT 0" clears. Carried in every beacon.
 *
 * Serial frame (unchanged):  AA 55 | type | len_lo len_hi | payload | xor
 *   type 0x01 = mole packet  (sender MAC 6 B + mole_pkt_t 234 B)
 *   type 0x02 = host status text (once per second)
 */

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_timer.h>

// ============================== USER CONFIG ==============================
#define ESPNOW_CHANNEL  1        // must match mole.ino
#define SERIAL_BAUD     921600   // must match moles_monitor.py --baud
#define CYCLE_US        100000   // beacon period = one full slot cycle
// ========================================================================

#define MOLE_MAGIC    0x4D
#define MOLE_PKT_LEN  234
#define FRAME_PKT     0x01
#define FRAME_TEXT    0x02

#define BEACON_MAGIC  0x42
typedef struct __attribute__((packed)) {
  uint8_t  magic;
  uint8_t  version;
  uint32_t cycle;
  uint8_t  alert_mask;
} beacon_t;

static const uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

typedef struct {
  uint8_t mac[6];
  uint8_t data[MOLE_PKT_LEN];
} rx_item_t;

static QueueHandle_t rx_queue;
static uint8_t my_mac[6];
static uint32_t n_rx = 0, n_drop = 0, n_bad = 0;     // written only by the WiFi task
static uint32_t cycle = 0, n_beacon_err = 0;
static uint8_t alert_mask = 0;

// ---- ESP-NOW receive (WiFi task): copy + queue only ----
static void handle_rx(const uint8_t *mac, const uint8_t *data, int len) {
  if (len != MOLE_PKT_LEN || data[0] != MOLE_MAGIC) { n_bad++; return; }
  rx_item_t item;
  memcpy(item.mac, mac, 6);
  memcpy(item.data, data, MOLE_PKT_LEN);
  if (xQueueSend(rx_queue, &item, 0) == pdTRUE) n_rx++;
  else n_drop++;
}

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
static void on_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) { handle_rx(info->src_addr, data, len); }
#else
static void on_recv(const uint8_t *mac, const uint8_t *data, int len) { handle_rx(mac, data, len); }
#endif

// ---- serial framing ----
static void send_frame(uint8_t type, const uint8_t *a, uint16_t na, const uint8_t *b, uint16_t nb) {
  uint16_t n = na + nb;
  uint8_t hdr[5] = {0xAA, 0x55, type, (uint8_t)(n & 0xFF), (uint8_t)(n >> 8)};
  uint8_t ck = hdr[2] ^ hdr[3] ^ hdr[4];
  for (uint16_t i = 0; i < na; i++) ck ^= a[i];
  for (uint16_t i = 0; i < nb; i++) ck ^= b[i];
  Serial.write(hdr, sizeof(hdr));
  if (na) Serial.write(a, na);
  if (nb) Serial.write(b, nb);
  Serial.write(&ck, 1);
}

static void send_status() {
  char line[160];
  int n = snprintf(line, sizeof(line),
                   "mac=%02X:%02X:%02X:%02X:%02X:%02X ch=%d cycle=%lu rx=%lu drop=%lu bad=%lu "
                   "beacon_err=%lu alert=0x%02X",
                   my_mac[0], my_mac[1], my_mac[2], my_mac[3], my_mac[4], my_mac[5], ESPNOW_CHANNEL,
                   (unsigned long)cycle, (unsigned long)n_rx, (unsigned long)n_drop, (unsigned long)n_bad,
                   (unsigned long)n_beacon_err, alert_mask);
  send_frame(FRAME_TEXT, (const uint8_t *)line, (uint16_t)n, nullptr, 0);
}

static void send_beacon() {
  beacon_t b = {BEACON_MAGIC, 1, cycle, alert_mask};
  if (esp_now_send(BROADCAST, (const uint8_t *)&b, sizeof(b)) != ESP_OK) n_beacon_err++;
  cycle++;
}

// ---- laptop -> host commands: "ALERT <mask>\n" ----
static void poll_commands() {
  static char buf[32];
  static uint8_t len = 0;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      buf[len] = 0;
      if (len > 6 && strncmp(buf, "ALERT ", 6) == 0) alert_mask = (uint8_t)(strtoul(buf + 6, nullptr, 0) & 0x07);
      len = 0;
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    }
  }
}

void setup() {
  Serial.setTxBufferSize(4096);
  Serial.begin(SERIAL_BAUD);
  delay(200);

  rx_queue = xQueueCreate(48, sizeof(rx_item_t));

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_get_mac(WIFI_IF_STA, my_mac);

  if (esp_now_init() != ESP_OK) {
    const char *err = "ESP-NOW init FAILED";
    while (1) { send_frame(FRAME_TEXT, (const uint8_t *)err, strlen(err), nullptr, 0); delay(1000); }
  }
  esp_now_register_recv_cb(on_recv);

  esp_now_peer_info_t peer = {};                  // broadcast peer for the beacon
  memcpy(peer.peer_addr, BROADCAST, 6);
  peer.channel = ESPNOW_CHANNEL;
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  esp_now_add_peer(&peer);

  send_status();
}

void loop() {
  static int64_t next_beacon = 0;
  static int64_t next_status = 0;
  int64_t now = esp_timer_get_time();

  if (now >= next_beacon) {                       // cycle master: fixed 100 ms grid, no drift
    send_beacon();
    next_beacon = (next_beacon == 0 || now - next_beacon > CYCLE_US) ? now + CYCLE_US : next_beacon + CYCLE_US;
  }

  rx_item_t item;
  while (xQueueReceive(rx_queue, &item, 0) == pdTRUE) {
    send_frame(FRAME_PKT, item.mac, 6, item.data, MOLE_PKT_LEN);
  }

  poll_commands();

  if (now >= next_status) {
    next_status = now + 1000000;
    send_status();
  }
  delay(1);                                       // yield; beacon jitter of ~1 ms shifts every mole equally
}
