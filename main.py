import time
import subprocess

print("Starting Angel One Live Option Chain Engine on Render...")

while True:
    try:
        subprocess.run(["python", "angel_login.py"])
    except Exception as e:
        print(f"Error running script: {e}")
    time.sleep(10)
