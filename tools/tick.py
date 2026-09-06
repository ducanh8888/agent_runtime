"""Append one line per second, so an observer can see exactly when it stops."""
import time

for i in range(90):
    with open("ticks.txt", "a") as fh:
        fh.write(str(i) + "\n")
    time.sleep(1)
