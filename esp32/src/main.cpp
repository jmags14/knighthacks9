#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <DHT.h>
#include "SPL07-003.h"   // Seeed_Arduino_SPA06 library (Grove SPA06-003 barometer)


// ---- Pins (Seeed XIAO RP2040, arduino-pico core: raw GPIO numbers) ----
#define WATER_PIN   A0   // GPIO26, ADC
#define MY_I2C_SDA  6
#define MY_I2C_SCL  7
#define DHT_PIN     0    // D6 (GPIO0)
#define HALL_PIN    1    // D7 (GPIO1)
#define BUZZER_PIN  2    // D8 (GPIO2)

#define BUZZER_ON   HIGH   // change to LOW for active-LOW modules
#define BUZZER_OFF  (!BUZZER_ON)

// ---- Barometer I2C address ----
// SPL07_ADDR_DEF = 0x77 (Grove default), SPL07_ADDR_ALT = 0x76
#define BARO_ADDR   SPL07_ADDR_DEF

// ---- Water calibration: note raw values dry and submerged (12-bit, 0-4095) ----
#define WATER_DRY   0
#define WATER_WET   2500

// ---- Rain categories (based on the water % above) ----
// CALIBRATE: watch "rain" in the output while dry, with a few drops, and soaked.
//   below RAIN_LIGHT_PCT           -> dry
//   RAIN_LIGHT_PCT .. HEAVY_PCT-1  -> light rain
//   RAIN_HEAVY_PCT and above       -> heavy rain
#define RAIN_LIGHT_PCT  10
#define RAIN_HEAVY_PCT  40

// ---- Rain history: one slot per minute, last 15 minutes ----
#define RAIN_WINDOW_MIN  15
#define MINUTE_MS        60000UL   // set to 10000UL to test quickly

// ---- Wind calibration: Hall pulses -> speed ----
// km/h per pulse-per-second (Hz). 2.4 is the common cup-anemometer value
// (one pulse/sec = 2.4 km/h). Adjust for your sensor and magnet count.
#define WIND_KMH_PER_HZ  2.4f

// ---- Alarm thresholds ----
#define WATER_ALARM_PCT  70
#define TEMP_ALARM_C     35

// Set to 0 to print only the JSON line
#define PRINT_HUMAN  1

DHT dht(DHT_PIN, DHT11);
SPL07_003 barometer;
bool barometerOK = false;

volatile uint32_t hallCount = 0;
void onHall() { hallCount++; }   // no IRAM_ATTR on RP2040

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

// Print a float with `dec` decimals, or the JSON literal null if it is NaN
void printJsonFloat(float v, int dec) {
  if (isnan(v)) Serial.print(F("null"));
  else Serial.print(v, dec);
}

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) { delay(10); }   // wait for USB serial monitor
  Serial.println(F("Sensor station starting..."));

  analogReadResolution(12);

  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, BUZZER_OFF);

  // RP2040: set pins first, then begin() with no arguments
  Wire.setSDA(MY_I2C_SDA);
  Wire.setSCL(MY_I2C_SCL);
  Wire.begin();

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

  // ---- Grove SPA06-003 barometer: find it, read its real chip ID, start it with that ID ----
  const uint8_t baroAddrs[2] = { BARO_ADDR, (uint8_t)(BARO_ADDR == SPL07_ADDR_DEF ? SPL07_ADDR_ALT : SPL07_ADDR_DEF) };
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
  attachInterrupt(HALL_PIN, onHall, FALLING);

  beep(1, 150);   // startup chirp
  minuteStart = millis();
  Serial.println(F("Ready."));
}

void loop() {
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

  // Wind: pulses since the last report -> Hz -> km/h
  uint32_t pulses = hallCount;
  uint32_t dPulses = pulses - lastPulses;
  lastPulses = pulses;
  float windKmh = (dPulses * 1000.0f / elapsedMs) * WIND_KMH_PER_HZ;

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
  if (waterAlarm) Serial.println(F("ALARM: water level high"));
  if (tempAlarm)  Serial.println(F("ALARM: temperature high"));
  if (minuteDone) printRainReport();
#endif

  // ---- JSON line ----
  Serial.print(F("{\"t\":\"r\",\"ms\":"));
  Serial.print(now);
  Serial.print(F(",\"p\":"));     printJsonFloat(pressure, 1);
  Serial.print(F(",\"temp\":"));  printJsonFloat(tempOut, 1);
  Serial.print(F(",\"hum\":"));   printJsonFloat(dhtOK ? h : NAN, 0);
  Serial.print(F(",\"water\":")); Serial.print(waterRaw);
  Serial.print(F(",\"rain\":"));  Serial.print(waterPct);
  Serial.print(F(",\"rainLvl\":\"")); Serial.print(RAIN_NAME[rainTrend]);
  Serial.print(F("\",\"rainMin\":")); Serial.print(rainMin);
  Serial.print(F(",\"wind\":"));  Serial.print(windKmh, 1);
  Serial.print(F(",\"wall\":"));  Serial.print(magnet ? 1 : 0);
  Serial.print(F(",\"src\":\"live\",\"alarms\":["));
  bool first = true;
  if (waterAlarm) { Serial.print(F("\"water\"")); first = false; }
  if (tempAlarm)  { if (!first) Serial.print(','); Serial.print(F("\"temp\"")); first = false; }
  Serial.println(F("]}"));

  // ---- Buzzer ----
  if (waterAlarm) {
    beep(3);
  } else if (tempAlarm) {
    beep(1, 500);
  } else if (magnet) {
    beep(1, 50);
  }
}