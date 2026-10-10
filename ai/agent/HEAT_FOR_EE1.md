# Heat index: notes for EE 1

The temperature and humidity sensor is **inside** the house, which is what matters when the AC is
off. The agent (Python) only *describes* the heat. **Alarms come only from the ESP32.** Please add
the heat index to the firmware and raise the existing `heat` alarm from it, so the box still works
when the Pi is down. The Pi then speaks the alarm using the event you already send:
`{"t":"alarm","id":"heat","level":"warn"|"alarm","state":"on"|"off"}`. No change to the event.

## The formula in Arduino C (NWS Rothfusz regression)

Source: https://www.wpc.ncep.noaa.gov/html/heatindex_equation.shtml. Same math as `ai/agent/heat.py`.

```c
#include <math.h>

// Heat index in degrees F from temperature in C and relative humidity in percent.
// Below ~80 F the result is about the air temperature, so treat anything under 80 as "no heat risk".
float heatIndexF(float tempC, float rh) {
  float T = tempC * 9.0f / 5.0f + 32.0f;
  if (rh < 0) rh = 0;
  if (rh > 100) rh = 100;

  // Simple formula first
  float hi = 0.5f * (T + 61.0f + (T - 68.0f) * 1.2f + rh * 0.094f);
  if ((hi + T) / 2.0f < 80.0f) return hi;

  // Full regression
  hi = -42.379f + 2.04901523f * T + 10.14333127f * rh
       - 0.22475541f * T * rh - 0.00683783f * T * T - 0.05481717f * rh * rh
       + 0.00122874f * T * T * rh + 0.00085282f * T * rh * rh
       - 0.00000199f * T * T * rh * rh;

  // Adjustments
  if (rh < 13 && T >= 80 && T <= 112) {
    hi -= ((13.0f - rh) / 4.0f) * sqrtf((17.0f - fabsf(T - 95.0f)) / 17.0f);
  } else if (rh > 85 && T >= 80 && T <= 87) {
    hi += ((rh - 85.0f) / 10.0f) * ((87.0f - T) / 5.0f);
  }
  return hi;
}
```
Use `float` and `sqrtf`/`fabsf`. Do not use integer math: the regression needs the decimals.

If the humidity reading is bad or missing, use the air temperature in F as the heat index and do not
guess a humidity.

### Check values (NWS chart, within 1 F)

| Temp F | RH % | Heat index F |
|---|---|---|
| 80 | 40 | 80 |
| 90 | 40 | 91 |
| 90 | 60 | 100 |
| 90 | 80 | 113 |
| 100 | 40 | 109 |
| 110 | 40 | 136 |

NWS categories: under 80 none, 80 to 90 Caution, 90 to 103 Extreme Caution, 103 to 125 Danger,
125 and up Extreme Danger.

## Suggested ESP32 rules

| Alarm level | Turns on when | Turns off when |
|---|---|---|
| `warn` | heat index >= 90 F for 10 minutes in a row | heat index < 88 F for 5 minutes |
| `alarm` | heat index >= 103 F for 10 minutes in a row | heat index < 100 F for 5 minutes |

- Sample the sensor as often as you already do. Compute the heat index each sample and keep a timer
  per level that resets to zero the moment the reading drops below the turn-on threshold. Do not
  average over the 10 minutes; "10 minutes in a row" is simpler and cannot hide a hot spike.
- The turn-off margin (2 to 3 F, 5 minutes) stops the alarm from flapping around the threshold.
- `alarm` replaces `warn` while it is on. When it clears but the heat index is still >= 90 F, drop back
  to `warn`.
- Send the event when the state changes, not on every sample. Keep the 16-character LCD lines from the
  Pi (`ai/agent/templates.py`: "Heat warning" / "Cool down") or use your own fallback when the Pi is down.
- Both thresholds are in F; keep them as constants at the top of the firmware so they are easy to tune.
