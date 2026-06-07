#!/usr/bin/python3
import cv2
import numpy as np
import time
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'open-fprintd-eh575'))
from egis_driver import egis_driver

SAVE_DIR = "/tmp/egis_debug"

def main():
    if not os.path.exists(SAVE_DIR):
        os.makedirs(SAVE_DIR)
        print(f"Created debug directory: {SAVE_DIR}")

    print("Initializing Driver...")
    try:
        driver = egis_driver.EgisDriver()
        driver._initialize_sensor()
    except Exception as e:
        print(f"Failed to init sensor: {e}")
        print("Did you forget to stop the egis-bridge service?")
        sys.exit(1)

    print("\n=== SENSOR DEBUG MODE ===")
    print(f"Saving images to: {SAVE_DIR}")
    print("Touch the sensor to capture images.")
    print("Press Ctrl+C to exit.\n")

    frame_count = 0

    try:
        while frame_count < 150:
            # Capture frame
            try:
                img_raw, contrast = driver.get_live_frame()
            except KeyboardInterrupt:
                print("Clean exit requested...")
                sys.exit(0)

            if img_raw is None:
                continue

            # Check if finger is present (Low threshold just to see everything)
            if contrast > 5.0:
                frame_count += 1

                # Convert raw bytes to Image
                # 50x103 is the dimension we saw in your matcher code
                img_arr = np.array(list(img_raw), dtype=np.uint8).reshape((52, 103))

                # Normalize it so it looks good (0-255)
                img_norm = cv2.normalize(img_arr, None, 0, 255, cv2.NORM_MINMAX)

                # Add a timestamp so filenames are unique
                timestamp = int(time.time() * 1000)
                filename = f"{SAVE_DIR}/scan_{timestamp}_contrast_{contrast:.1f}.png"

                cv2.imwrite(filename, img_norm)
                print(f"Saved {filename} (Contrast: {contrast:.1f})")

                # Tiny sleep to prevent filling your disk instantly
                time.sleep(0.1)
            else:
                # Print idle contrast every once in a while so you know it's alive
                if frame_count % 20 == 0:
                    sys.stdout.write(f"\rIdle... Contrast: {contrast:.2f}   ")
                    sys.stdout.flush()
                time.sleep(0.05)

    except KeyboardInterrupt:
        print("\nExiting.")

if __name__ == "__main__":
    main()