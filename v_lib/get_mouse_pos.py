import os
if 'DISPLAY' not in os.environ:
    os.environ['DISPLAY'] = ':99'

import pyautogui
import sys
import time

def main():
    print("Mouse Position Tracker")
    print("Press Ctrl+C to stop.")
    print("-" * 20)
    try:
        while True:
            x, y = pyautogui.position()
            position_str = f"X: {str(x).rjust(4)} Y: {str(y).rjust(4)}"
            print(position_str, end="")
            print("\b" * len(position_str), end="", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nAborted.")

if __name__ == "__main__":
    main()
