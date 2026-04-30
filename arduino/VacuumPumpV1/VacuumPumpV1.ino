/*
 * VacuumPumpV1 — Arduino Uno vacuum motor + MPX5100DP pressure stream
 *
 * Serial @ 9600 baud. Matches the Python GUI vacuum panel.
 *
 * Commands from GUI:
 *   '1'  -> motor + LED ON,  replies "MOTOR:ON"
 *   '0'  -> motor + LED OFF, replies "MOTOR:OFF"
 *
 * Continuous telemetry (10 Hz, always while connected — independent of motor state):
 *   VACUUM_KPA:<kpa>,INHG:<inhg>
 *
 * Wiring:
 *   motor relay/transistor signal -> D9
 *   indicator LED (with ~220 ohm resistor) -> D3
 *   MPX5100DP analog output       -> A0
 */

const int motorPin = 9;
const int ledPin = 3;
const int sensorPin = A0;

// Sensor calibration (MPX5100DP, ratiometric to 5V)
const float V_SUPPLY = 5.0;
const float V_MIN = 0.2;      // voltage at 0 kPa differential
const float V_MAX = 4.7;      // voltage at 100 kPa differential
const float P_MAX = 100.0;    // kPa full scale

const float KPA_TO_INHG = 0.2953;

bool motorState = false;

unsigned long lastPrintTime = 0;
const unsigned long printInterval = 100; // ms (10 Hz)

void setup() {
  pinMode(motorPin, OUTPUT);
  pinMode(ledPin, OUTPUT);

  digitalWrite(motorPin, LOW);
  digitalWrite(ledPin, LOW);

  Serial.begin(9600);
}

void loop() {
  // Handle GUI commands
  if (Serial.available() > 0) {
    char cmd = Serial.read();

    if (cmd == '1') {
      digitalWrite(motorPin, HIGH);
      digitalWrite(ledPin, HIGH);
      motorState = true;
      Serial.println("MOTOR:ON");
    }
    else if (cmd == '0') {
      digitalWrite(motorPin, LOW);
      digitalWrite(ledPin, LOW);
      motorState = false;
      Serial.println("MOTOR:OFF");
    }
  }

  // Stream vacuum readings continuously (lets the GUI monitor vacuum hold
  // after the pump shuts off, and reads ~0 kPa when no vacuum is applied).
  if (millis() - lastPrintTime >= printInterval) {
    lastPrintTime = millis();

    int rawTotal = 0;
    const int numSamples = 5;
    for (int i = 0; i < numSamples; i++) {
      rawTotal += analogRead(sensorPin);
    }

    float rawAvg = rawTotal / (float)numSamples;
    float voltage = rawAvg * (V_SUPPLY / 1023.0);

    float pressure_kPa = (voltage - V_MIN) * (P_MAX / (V_MAX - V_MIN));
    if (pressure_kPa < 0) pressure_kPa = 0;
    if (pressure_kPa > P_MAX) pressure_kPa = P_MAX;

    float vacuum_kPa = pressure_kPa;
    float vacuum_inHg = vacuum_kPa * KPA_TO_INHG;

    Serial.print("VACUUM_KPA:");
    Serial.print(vacuum_kPa, 2);
    Serial.print(",INHG:");
    Serial.println(vacuum_inHg, 2);
  }
}
