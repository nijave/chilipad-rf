// Isolate the D2<->D6 coupling.
#include <Arduino.h>
#include <ESP8266WiFi.h>
static const uint8_t CE = D2, CSN = D8, SCK_ = D5, MOSI_ = D7, MISO_ = D6;
void setup() {
  WiFi.mode(WIFI_OFF);
  Serial.begin(115200);
  delay(150);
}
static int rdPU(uint8_t p) { pinMode(p, INPUT_PULLUP); delayMicroseconds(200); return digitalRead(p); }
void loop() {
  // A: every line high-Z with pull-ups -> who reads what
  for (uint8_t p : {CE, CSN, SCK_, MOSI_, MISO_}) pinMode(p, INPUT_PULLUP);
  delay(2);
  int a_ce = digitalRead(CE), a_miso = digitalRead(MISO_), a_csn = digitalRead(CSN),
      a_sck = digitalRead(SCK_), a_mosi = digitalRead(MOSI_);
  // B: only D2 driven low, rest high-Z pull-up
  pinMode(CE, OUTPUT); digitalWrite(CE, LOW); delay(2);
  int b_miso = rdPU(MISO_);
  // C: only D6 driven low, read D2
  pinMode(CE, INPUT_PULLUP); pinMode(MISO_, OUTPUT); digitalWrite(MISO_, LOW); delay(2);
  int c_ce = rdPU(CE);
  pinMode(MISO_, INPUT_PULLUP);
  // D: GPIO numbers actually used
  Serial.printf("A(all hi-Z PU): D2=%d D6=%d D8=%d D5=%d D7=%d | B(D2 low): D6=%d | C(D6 low): D2=%d | "
                "GPIO D2=%u D6=%u\r\n", a_ce, a_miso, a_csn, a_sck, a_mosi, b_miso, c_ce, CE, MISO_);
  delay(1000);
}
