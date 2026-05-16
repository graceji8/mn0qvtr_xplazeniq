#!/usr/bin/env python3

import os
os.environ['DISPLAY'] = ':99'
import time
from datetime import datetime
import pyautogui
from PIL import ImageGrab

def get_mouse_position():
    """Get the current mouse position using pyautogui"""
    try:
        x, y = pyautogui.position()
        return x, y
    except Exception as e:
        print(f"Error getting mouse position: {e}")
        return None, None

def wait_for_input(message):
    """Wait for user to press Enter"""
    input(f"Press Enter to capture {message}...")

def main():
    print("Screenshot Region Capture Tool")
    print("=" * 50)
    print("Note: Move your mouse to the corners and press Enter")
    print("=" * 50)
    
    # Get top-left corner
    wait_for_input("top-left corner")
    top_left_x, top_left_y = get_mouse_position()
    if top_left_x is None:
        print("Failed to get top-left coordinates")
        return
    print(f"Top-left corner: ({top_left_x}, {top_left_y})")
    
    # Get bottom-right corner
    wait_for_input("bottom-right corner")
    bottom_right_x, bottom_right_y = get_mouse_position()
    if bottom_right_x is None:
        print("Failed to get bottom-right coordinates")
        return
    print(f"Bottom-right corner: ({bottom_right_x}, {bottom_right_y})")
    
    # Calculate dimensions
    width = bottom_right_x - top_left_x
    height = bottom_right_y - top_left_y
    
    # Validate selection
    if width <= 0 or height <= 0:
        print("Error: Invalid selection. Make sure bottom-right is below and to the right of top-left.")
        return
    
    print(f"Region dimensions: {width}x{height}")
    
    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"screenshot_{timestamp}.png"
    
    try:
        print("Capturing screenshot in 1 second...")
        time.sleep(1)  # Give time for any windows to refresh
        
        # Method 1: Try PyAutoGUI first
        try:
            screenshot = pyautogui.screenshot(region=(top_left_x, top_left_y, width, height))
            screenshot.save(filename)
            print(f"✓ Screenshot saved as: {filename}")
            print(f"Location: {os.path.abspath(filename)}")
            return
        except Exception as e:
            print(f"PyAutoGUI failed: {e}")
            print("Trying alternative method...")
        
        # Method 2: Try PIL ImageGrab (works better on Windows)
        try:
            bbox = (top_left_x, top_left_y, bottom_right_x, bottom_right_y)
            screenshot = ImageGrab.grab(bbox=bbox)
            screenshot.save(filename)
            print(f"✓ Screenshot saved as: {filename}")
            print(f"Location: {os.path.abspath(filename)}")
            return
        except Exception as e:
            print(f"ImageGrab failed: {e}")
        
        # Method 3: Full screen capture then crop
        try:
            print("Trying full screen capture with crop...")
            screenshot = ImageGrab.grab()
            cropped = screenshot.crop((top_left_x, top_left_y, bottom_right_x, bottom_right_y))
            cropped.save(filename)
            print(f"✓ Screenshot saved as: {filename}")
            print(f"Location: {os.path.abspath(filename)}")
            return
        except Exception as e:
            print(f"Full screen capture failed: {e}")
            
        print("❌ All capture methods failed")
        
    except Exception as e:
        print(f"Error capturing screenshot: {e}")

if __name__ == "__main__":
    main()