#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <Adafruit_Sensor.h>
#include <DHT.h>
#include "SPL07-003.h"   // Seeed_Arduino_SPA06 library (Grove SPA06-003 barometer)

// =====================================================================
// Network: the ESP32 is the access point, the laptop joins it and runs
// Mosquitto. The ESP32 itself is at 192.168.4.1; the laptop normally
// gets 192.168.4.2 (set it as a static IP on the laptop to be sure).
// =====================================================================
#define AP_SSID      "weathertest"
#define AP_PASS      "weather1234"      // must be 8+ characters
#define MQTT_HOST    "192.168.4.2"      // laptop's IP on the ESP32 network
#define MQTT_PORT    1883
#define MQTT_USER    "station"
#define MQTT_PASS    "0918"
#define MQTT_CLIENT  "esp32-weather"

#define TOPIC_DATA    "weather/station"   // JSON reading every 2 s
#define TOPIC_STATUS  "weather/status"    // "online" / "offline" (retained)
#define TOPIC_CMD     "weather/cmd"       // send "beep" to test the buzzer

#define WIND_MS_PER_HZ   0.667f

// =====================================================================
// ESP32-WROOM-32D DevKit pin map (matches pinout.xlsx)
// The numbers below are GPIO numbers. On the board silkscreen the back
// labels them P<n> (e.g. P21) and the front may show D<n> (e.g. D21).
//
//   Function            GPIO   Board label   Module
//   ------------------  -----  -----------   ---------------------------
//   I2C SDA             21     P21 / D21     Grove SPA06-003 (white)
//   I2C SCL             22     P22 / D22     Grove SPA06-003 (yellow)
//   Water level (ADC)   34     P34 / D34     Water sensor signal
//   DHT11 data          4      P4  / D4      DHT11
//   Hall / wind pulse   27     P27 / D27     Hall sensor (A3144 / KY-003)
//   Buzzer              26     P26 / D26     Buzzer module
//   Power               -      3V3           All sensors
//   Ground              -      GND           All sensors
// =====================================================================
#define MY_I2C_SDA  21   // P21
#define MY_I2C_SCL  22   // P22
#define WATER_PIN   34   // P34, ADC1_CH6, input-only
#define DHT_PIN     4    // P4
#define HALL_PIN    27   // P27
#define BUZZER_PIN  26   // P26
// Avoid: GPIO6-11 (flash), GPIO0/2/12/15 (boot strapping), GPIO34-39 have no pull-ups.

#define BUZZER_ON   HIGH   // change to LOW for active-LOW modules
#define BUZZER_OFF  (!BUZZER_ON)

// ---- Barometer I2C address ----
// SPL07_ADDR_DEF = 0x77 (Grove default), SPL07_ADDR_ALT = 0x76
#define BARO_ADDR   SPL07_ADDR_DEF

// ---- Water calibration: raw values dry and submerged (12-bit, 0-4095) ----
// NOTE: ESP32 ADC is non-linear near 0 V and 3.3 V; recalibrate these values.
#define WATER_DRY   0
#define WATER_WET   2500

// ---- Rain categories (based on the water %) ----
#define RAIN_LIGHT_PCT  10
#define RAIN_HEAVY_PCT  40

// ---- Rain history: one slot per minute, last 15 minutes ----
#define RAIN_WINDOW_MIN  15
#define MINUTE_MS        60000UL   // set to 10000UL to test quickly

// ---- Wind calibration: Hall pulses -> speed ----
#define WIND_KMH_PER_HZ  2.4f

// ---- Alarm thresholds ----
#define WATER_ALARM_PCT  70
#define TEMP_ALARM_C     35

// Set to 0 to print only the JSON line
#define PRINT_HUMAN  1

DHT dht(DHT_PIN, DHT11);
SPL07_003 barometer;
bool barometerOK = false;

WiFiClient espClient;
PubSubClient mqtt(espClient);
uint32_t lastMqttAttempt = 0;

volatile uint32_t hallCount = 0;
void IRAM_ATTR onHall() { hallCount++; }   // ISR must live in IRAM on ESP32

// ---- Rain tracking state ----
enum RainLevel { RAIN_DRY = 0, RAIN_LIGHT = 1, RAIN_HEAVY = 2 };
const char* const RAIN_NAME[]  = { "dry", "light", "heavy" };
const char* const RAIN_LABEL[] = { "Dry", "Light rain", "Heavy rain" };

RainLevel rainHistory[RAIN_WINDOW_MIN];   // ring buffer of completed minutes
int rainCount = 0;                        // completed minutes stored (max 15)
int rainHead  = 0;                        // next slot to write
long minuteSum = 0;                       // sum of water % samples this minute
int  minuteSamples = 0;
uint32_t minuteStart = 0;

RainLevel classifyRain(int pct) {
  if (pct >= RAIN_HEAVY_PCT) return RAIN_HEAVY;
  if (pct >= RAIN_LIGHT_PCT) return RAIN_LIGHT;
  return RAIN_DRY;
}

// n = 0 is the most recent completed minute, 1 the one before, ...
RainLevel rainMinuteAgo(int n) {
  int idx = (rainHead - 1 - n + RAIN_WINDOW_MIN * 2) % RAIN_WINDOW_MIN;
  return rainHistory[idx];
}

// Consecutive completed minutes the latest category has lasted (capped at 15)
int rainStreak() {
  if (rainCount == 0) return 0;
  RainLevel cur = rainMinuteAgo(0);
  int s = 0;
  while (s < rainCount && rainMinuteAgo(s) == cur) s++;
  return s;
}

void printRainReport() {
  if (rainCount == 0) {
    Serial.println(F("RAIN REPORT: collecting the first minute of data..."));
    return;
  }
  RainLevel cur = rainMinuteAgo(0);
  int streak = rainStreak();
  int cnt[3] = {0, 0, 0};
  for (int i = 0; i < rainCount; i++) cnt[rainMinuteAgo(i)]++;

  Serial.print(F("RAIN REPORT: "));
  Serial.print(RAIN_LABEL[cur]);
  if (streak >= RAIN_WINDOW_MIN) {
    Serial.print(F(" for the past 15 minutes"));
  } else {
    Serial.print(F(" for the past "));
    Serial.print(streak);
    Serial.print(streak == 1 ? F(" minute") : F(" minutes"));
  }
  Serial.print(F("  (last "));
  Serial.print(rainCount);
  Serial.print(F(" min: heavy "));
  Serial.print(cnt[RAIN_HEAVY]);
  Serial.print(F(", light "));
  Serial.print(cnt[RAIN_LIGHT]);
  Serial.print(F(", dry "));
  Serial.print(cnt[RAIN_DRY]);
  Serial.println(F(")"));
}

int readAvg(int pin, int samples = 16) {
  long sum = 0;
  for (int i = 0; i < samples; i++) { sum += analogRead(pin); delay(2); }
  return sum / samples;
}

void beep(int times, int onMs = 100, int offMs = 100) {
  for (int i = 0; i < times; i++) {
    digitalWrite(BUZZER_PIN, BUZZER_ON);
    delay(onMs);
    digitalWrite(BUZZER_PIN, BUZZER_OFF);
    if (i < times - 1) delay(offMs);
  }
}

// Read one register from an I2C device (0xFF on failure)
uint8_t readReg(uint8_t addr, uint8_t reg) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return 0xFF;
  if (Wire.requestFrom(addr, (uint8_t)1) != 1) return 0xFF;
  return Wire.read();
}

// Format a float with `dec` decimals, or the JSON literal null if it is NaN
void fmtJsonFloat(char* out, size_t n, float v, int dec) {
  if (isnan(v)) snprintf(out, n, "null");
  else          snprintf(out, n, "%.*f", dec, v);
}

void printPinMap() {
  Serial.println(F("Pin map (GPIO / board label):"));
  Serial.printf("  I2C SDA  : GPIO%d (P%d)\n", MY_I2C_SDA, MY_I2C_SDA);
  Serial.printf("  I2C SCL  : GPIO%d (P%d)\n", MY_I2C_SCL, MY_I2C_SCL);
  Serial.printf("  Water    : GPIO%d (P%d)\n", WATER_PIN, WATER_PIN);
  Serial.printf("  DHT11    : GPIO%d (P%d)\n", DHT_PIN, DHT_PIN);
  Serial.printf("  Hall     : GPIO%d (P%d)\n", HALL_PIN, HALL_PIN);
  Serial.printf("  Buzzer   : GPIO%d (P%d)\n", BUZZER_PIN, BUZZER_PIN);
}

// ---- MQTT ----
void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  String msg;
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
  Serial.printf("MQTT in [%s]: %s\n", topic, msg.c_str());
  if (msg == "beep") beep(1, 150);
}

// Non-blocking: one attempt every 3 s, and only once a client has joined the AP
void mqttEnsureConnected() {
  if (mqtt.connected()) return;
  if (millis() - lastMqttAttempt < 3000) return;
  lastMqttAttempt = millis();

  if (WiFi.softAPgetStationNum() == 0) {
    Serial.println(F("MQTT: waiting for the laptop to join the access point..."));
    return;
  }

  Serial.printf("MQTT: connecting to %s:%d ...", MQTT_HOST, MQTT_PORT);
  // last-will message: broker publishes "offline" if the ESP32 drops
  if (mqtt.connect(MQTT_CLIENT, MQTT_USER, MQTT_PASS, TOPIC_STATUS, 0, true, "offline")) {
    Serial.println(F(" connected"));
    mqtt.publish(TOPIC_STATUS, "online", true);
    mqtt.subscribe(TOPIC_CMD);
  } else {
    Serial.printf(" failed, rc=%d\n", mqtt.state());
  }
}

void setup() {
  Serial.begin(115200);
  delay(1500);   // ESP32 USB-UART: give the monitor time to attach
  Serial.println(F("Sensor station starting (ESP32)..."));
  printPinMap();

  analogReadResolution(12);
  analogSetPinAttenuation(WATER_PIN, ADC_11db);   // full 0-3.3 V range

  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, BUZZER_OFF);

  // ---- Wi-Fi access point (do NOT call WiFi.begin anywhere) ----
  WiFi.mode(WIFI_AP);
  bool apOK = WiFi.softAP(AP_SSID, AP_PASS);
  Serial.printf("Access point '%s' %s, IP %s\n", AP_SSID,
                apOK ? "started" : "FAILED to start",
                WiFi.softAPIP().toString().c_str());

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  mqtt.setBufferSize(512);   // JSON line is ~200 bytes; default 256 is tight

  // ESP32: pins are passed to begin()
  Wire.begin(MY_I2C_SDA, MY_I2C_SCL);
  Wire.setClock(100000);

  // ---- I2C scan: list every device on the bus ----
  Serial.print(F("I2C scan (SDA="));
  Serial.print(MY_I2C_SDA);
  Serial.print(F(", SCL="));
  Serial.print(MY_I2C_SCL);
  Serial.print(F("):"));
  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf(" 0x%02X", a);
      found++;
    }
  }
  if (found == 0) Serial.print(F(" nothing found - check wiring/power"));
  Serial.println();

  // ---- Grove SPA06-003 barometer: find it, read its real chip ID, start with that ID ----
  const uint8_t baroAddrs[2] = { BARO_ADDR,
    (uint8_t)(BARO_ADDR == SPL07_ADDR_DEF ? SPL07_ADDR_ALT : SPL07_ADDR_DEF) };
  for (int i = 0; i < 2 && !barometerOK; i++) {
    uint8_t addr = baroAddrs[i];
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() != 0) continue;   // nothing at this address

    uint8_t id = readReg(addr, 0x0D);            // product/revision ID register
    Serial.printf("Barometer at 0x%02X, chip ID 0x%02X (library expects 0x%02X)\n",
                  addr, id, SPL07_EXPECTED_ID);

    barometerOK = barometer.begin(addr, &Wire, id);   // accept the ID the chip reports
    if (!barometerOK) Serial.println(F("  begin() still failed at this address"));
  }

  if (barometerOK) {
    barometer.setPressureConfig(SPL07_4HZ, SPL07_32SAMPLES);
    barometer.setTemperatureConfig(SPL07_4HZ, SPL07_1SAMPLE);
    barometer.setMode(SPL07_CONT_PRES_TEMP);   // continuous measurements
    Serial.println(F("SPA06-003 detected"));
  } else {
    Serial.println(F("SPA06-003 not detected"));
  }

  dht.begin();

  pinMode(HALL_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(HALL_PIN), onHall, FALLING);

  beep(1, 150);   // startup chirp
  minuteStart = millis();
  Serial.println(F("Ready."));
}

void loop() {
  // Keep MQTT alive on every pass, before the 2 s sensor gate below
  mqttEnsureConnected();
  mqtt.loop();

  static uint32_t last = 0;
  static uint32_t lastPulses = 0;
  uint32_t now = millis();
  if (now - last < 2000) return;   // DHT11 needs >= 1-2 s between reads
  uint32_t elapsedMs = now - last;
  last = now;

  // Temp / humidity
  float h  = dht.readHumidity();
  float tC = dht.readTemperature();
  float tF = dht.readTemperature(true);
  bool dhtOK = !(isnan(h) || isnan(tC) || isnan(tF));
  float heatIdx = dhtOK ? dht.computeHeatIndex(tC, h, false) : NAN;

  // Barometer (library returns Pa and deg C)
  float pressure = NAN;
  float baroTemp = NAN;
  if (barometerOK) {
    pressure = (float)(barometer.readPressure() / 100.0);   // Pa -> hPa
    baroTemp = (float)barometer.readTemperature();
  }

  // Water
  int waterRaw = readAvg(WATER_PIN);
  int waterPct = constrain((waterRaw - WATER_DRY) * 100 / (WATER_WET - WATER_DRY), 0, 100);

  // ---- Rain category: instant + once-a-minute history ----
  RainLevel rainNow = classifyRain(waterPct);
  minuteSum += waterPct;
  minuteSamples++;
  bool minuteDone = false;
  if (now - minuteStart >= MINUTE_MS) {
    minuteStart = now;
    int avgPct = minuteSamples ? (int)(minuteSum / minuteSamples) : 0;
    rainHistory[rainHead] = classifyRain(avgPct);
    rainHead = (rainHead + 1) % RAIN_WINDOW_MIN;
    if (rainCount < RAIN_WINDOW_MIN) rainCount++;
    minuteSum = 0;
    minuteSamples = 0;
    minuteDone = true;
  }
  int rainMin = rainStreak();   // minutes the latest category has lasted
  RainLevel rainTrend = rainCount ? rainMinuteAgo(0) : rainNow;

  // Hall (A3144/KY-003 pulls LOW near a magnet)
  bool magnet = digitalRead(HALL_PIN) == LOW;

  // Wind: pulses since the last report -> Hz -> m/s
  uint32_t pulses = hallCount;
  uint32_t dPulses = pulses - lastPulses;
  lastPulses = pulses;
  float windMs = (dPulses * 1000.0f / elapsedMs) * WIND_MS_PER_HZ;   // m/s

  // Alarm flags
  bool waterAlarm = waterPct >= WATER_ALARM_PCT;
  bool tempAlarm  = dhtOK && tC >= TEMP_ALARM_C;

  // Best available temperature: DHT11 first, barometer as fallback
  float tempOut = dhtOK ? tC : baroTemp;

#if PRINT_HUMAN
  Serial.println(F("------------------------"));
  if (dhtOK) {
    Serial.print(F("DHT11    : "));
    Serial.print(h, 0);  Serial.print(F("% RH, "));
    Serial.print(tC, 1); Serial.print(F(" C ("));
    Serial.print(tF, 1); Serial.print(F(" F), heat index "));
    Serial.print(heatIdx, 1); Serial.println(F(" C"));
  } else {
    Serial.println(F("DHT11    : read failed, check wiring"));
  }
  if (barometerOK) {
    Serial.print(F("SPA06-003: "));
    Serial.print(pressure, 1);
    Serial.print(F(" hPa, "));
    Serial.print(baroTemp, 1);
    Serial.println(F(" C"));
  } else {
    Serial.println(F("SPA06-003: not detected"));
  }
  Serial.printf("Water    : raw %d, %d %%  -> now: %s\n", waterRaw, waterPct, RAIN_NAME[rainNow]);
  Serial.printf("Hall     : %s, pulses %lu\n", magnet ? "MAGNET" : "none", (unsigned long)hallCount);
  Serial.printf("Wi-Fi    : %d client(s) on AP, MQTT %s\n",
                WiFi.softAPgetStationNum(), mqtt.connected() ? "connected" : "disconnected");
  if (waterAlarm) Serial.println(F("ALARM: water level high"));
  if (tempAlarm)  Serial.println(F("ALARM: temperature high"));
  if (minuteDone) printRainReport();
#endif

  // ---- Build the JSON line ----
  char sP[16], sTemp[16], sHum[16];
  fmtJsonFloat(sP,    sizeof(sP),    pressure,            1);
  fmtJsonFloat(sTemp, sizeof(sTemp), tempOut,             1);
  fmtJsonFloat(sHum,  sizeof(sHum),  dhtOK ? h : NAN,     0);

  char alarms[32];
  alarms[0] = '\0';
  if (waterAlarm) strcat(alarms, "\"water\"");
  if (tempAlarm) {
    if (alarms[0]) strcat(alarms, ",");
    strcat(alarms, "\"temp\"");
  }

  char json[320];
  snprintf(json, sizeof(json),
           "{\"t\":\"r\",\"ms\":%lu,\"p\":%s,\"temp\":%s,\"hum\":%s,"
           "\"water\":%d,\"rain\":%d,\"rainLvl\":\"%s\",\"rainMin\":%d,"
           "\"wind\":%.2f,\"wall\":%d,\"src\":\"live\",\"alarms\":[%s]}",
           (unsigned long)now, sP, sTemp, sHum,
           waterRaw, waterPct, RAIN_NAME[rainTrend], rainMin,
           windMs, magnet ? 1 : 0, alarms);

  Serial.println(json);                       // still goes out over USB serial
  if (mqtt.connected()) {
    mqtt.publish(TOPIC_DATA, json);           // and over MQTT to the laptop
  }

  // ---- Buzzer ----
  if (waterAlarm) {
    beep(3);
  } else if (tempAlarm) {
    beep(1, 500);
  } else if (magnet) {
    beep(1, 50);
  }
}
