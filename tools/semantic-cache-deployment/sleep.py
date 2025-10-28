import time
import sys

seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 30
print(f"Sleeping for {seconds} seconds...")
time.sleep(seconds)
print("Done sleeping")