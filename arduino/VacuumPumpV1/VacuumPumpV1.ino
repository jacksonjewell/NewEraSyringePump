/*
 * VacuumPumpV1 — Arduino Uno vacuum motor control
 *
 * Listens on Serial at 9600 baud. Send ASCII '1' to turn motor + LED ON,
 * ASCII '0' to turn OFF. Matches the Python GUI vacuum panel behavior.
 */

const int motorPin = 9;
const int ledPin = 3;

void setup() {
  pinMode(motorPin, OUTPUT);
  pinMode(ledPin, OUTPUT);
  Serial.begin(9600);
}

void loop() {
  if (Serial.available() > 0) {
    char cmd = Serial.read();

    if (cmd == '1') {
      digitalWrite(motorPin, HIGH);
      digitalWrite(ledPin, HIGH);
      Serial.println("Motor ON");
    }

    if (cmd == '0') {
      digitalWrite(motorPin, LOW);
      digitalWrite(ledPin, LOW);
      Serial.println("Motor OFF");
    }
  }
}
