/*
 * MOLES — Phase 1: Hand-Wave Test
 * HOST ESP32 — flash this onto the ESP32 plugged into the laptop
 *
 * Receives mole_pkt_t over ESP-NOW and forwards it to the laptop over USB serial
 * (921600 baud) as binary frames. moles_monitor.py decodes and prints them.
 *
 * Serial frame:  AA 55 | type(1) | len(2, little-endian) | payload(len) | xor-checksum(1)
 *   type 0x01 = mole packet   payload = sender MAC (6) + mole_pkt_t (234)
 *   type 0x02 = host text     payload = ASCII status line (sent once per second)
 * The checksum is the XOR of type, both len bytes, and every payload byte.
 *
 * No UWB module on this board. Arduino-ESP32 core 2.x or 3.x.
 */

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

// ============================== USER CONFIG ==============================
#define ESPNOW_CHANNEL  1        // Must match mole_a_tx.ino
#define SERIAL_BAUD     921600   // Must match moles_monitor.py --baud
// ========================================================================

#define MOLE_MAGIC     0x4D
#define MOLE_PKT_LEN   234
#define FRAME_PKT      0x01
#define FRAME_TEXT     0x02

typedef struct {
  uint8_t mac[6];
  uint8_t data[MOLE_PKT_LEN];
} rx_item_t;

static QueueHandle_t rx_queue;
static uint8_t my_mac[6];
static uint32_t n_rx = 0, n_drop = 0, n_bad = 0;   // written only by the WiFi task

// ---- ESP-NOW receive (runs in the WiFi task: copy + queue only, no Serial here) ----
static void handle_rx(const uint8_t *mac, const uint8_t *data, int len) {
  if (len != MOLE_PKT_LEN || data[0] != MOLE_MAGIC) { n_bad++; return; }
  rx_item_t item;
  memcpy(item.mac, mac, 6);
  memcpy(item.data, data, MOLE_PKT_LEN);
  if (xQueueSend(rx_queue, &item, 0) == pdTRUE) n_rx++;
  else n_drop++;
}

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
static void on_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  handle_rx(info->src_addr, data, len);
}
#else
static void on_recv(const uint8_t *mac, const uint8_t *data, int len) {
  handle_rx(mac, data, len);
}
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
  char line[128];
  int n = snprintf(line, sizeof(line),
                   "mac=%02X:%02X:%02X:%02X:%02X:%02X ch=%d rx=%lu drop=%lu bad=%lu",
                   my_mac[0], my_mac[1], my_mac[2], my_mac[3], my_mac[4], my_mac[5],
                   ESPNOW_CHANNEL, (unsigned long)n_rx, (unsigned long)n_drop, (unsigned long)n_bad);
  send_frame(FRAME_TEXT, (const uint8_t *)line, (uint16_t)n, nullptr, 0);
}

void setup() {
  Serial.setTxBufferSize(4096);
  Serial.begin(SERIAL_BAUD);
  delay(200);

  rx_queue = xQueueCreate(32, sizeof(rx_item_t));

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_get_mac(WIFI_IF_STA, my_mac);

  if (esp_now_init() != ESP_OK) {
    const char *err = "ESP-NOW init FAILED";
    while (1) { send_frame(FRAME_TEXT, (const uint8_t *)err, strlen(err), nullptr, 0); delay(1000); }
  }
  esp_now_register_recv_cb(on_recv);

  send_status();
}

void loop() {
  static uint32_t next_status = 0;
  rx_item_t item;

  while (xQueueReceive(rx_queue, &item, 0) == pdTRUE) {
    send_frame(FRAME_PKT, item.mac, 6, item.data, MOLE_PKT_LEN);
  }

  uint32_t now = millis();
  if ((int32_t)(now - next_status) >= 0) {
    next_status = now + 1000;
    send_status();
  }

  delay(1);
}
