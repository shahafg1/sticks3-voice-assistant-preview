# main.py - auto-start the voice assistant on power-on.
# Runs with boot_option 0, where UIFlow skips networking, so Wi-Fi is
# connected here from the credentials saved by the UIFlow Setup app.
import M5
import esp32
import sys
import network
import time

WIFI_TIMEOUT_S = 20
RETRY_AFTER_S = 30
APP = "/flash/voice_send.py"


def show(*lines):
    M5.Lcd.fillScreen(0x000000)
    M5.Lcd.setTextColor(0x00FF00)
    M5.Lcd.setTextSize(2)
    for i, text in enumerate(lines):
        M5.Lcd.drawString(text, 5, 30 + i * 25)


def wait_release():
    while M5.BtnA.isPressed():
        M5.update()
        time.sleep_ms(20)


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return wlan
    nvs = esp32.NVS("uiflow")
    ssid = nvs.get_str("ssid0")
    if not ssid:
        show("No saved", "Wi-Fi.", "Use UIFlow", "Setup")
        return None
    try:
        wlan.disconnect()
    except OSError:
        pass
    wlan.connect(ssid, nvs.get_str("pswd0"))
    start = time.ticks_ms()
    while not wlan.isconnected():
        elapsed = time.ticks_diff(time.ticks_ms(), start) // 1000
        if elapsed >= WIFI_TIMEOUT_S or wlan.status() == network.STAT_WRONG_PASSWORD:
            failure = "Bad password" if wlan.status() == network.STAT_WRONG_PASSWORD else "Timed out"
            wlan.disconnect()
            show("Wi-Fi failed", failure)
            time.sleep(2)
            return None
        show("Wi-Fi...", str(WIFI_TIMEOUT_S - elapsed) + "s")
        time.sleep_ms(500)
    return wlan


def wait_for_retry():
    start = time.ticks_ms()
    while True:
        left = RETRY_AFTER_S - time.ticks_diff(time.ticks_ms(), start) // 1000
        if left <= 0:
            return
        show("No Wi-Fi", "Press=retry", "Auto " + str(left) + "s")
        for _ in range(10):
            M5.update()
            if M5.BtnA.isPressed():
                wait_release()
                return
            time.sleep_ms(100)


M5.begin()
show("Starting...")
while True:
    wlan = connect_wifi()
    if wlan:
        break
    wait_for_retry()

show("Wi-Fi OK", wlan.ifconfig()[0])
time.sleep(1)
wait_release()
print("Wi-Fi connected:", wlan.ifconfig()[0])

try:
    exec(open(APP).read())
except Exception as error:
    show("App error", "See USB log")
    print("Voice app error:", error)
    sys.print_exception(error)
