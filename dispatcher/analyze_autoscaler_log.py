
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime
import os

# Paths
CSV_PATH = "autoscaler_log.csv"
OUTPUT_PLOT = "autoscaler_performance_plot.png"

# Load log
df = pd.read_csv(CSV_PATH, parse_dates=["Timestamp"])
df = df.dropna()

# Clean up
df = df[df["P99_Latency"] != "N/A"]
df = df[df["Queue_Size"] != "N/A"]
df["P99_Latency"] = df["P99_Latency"].astype(float)
df["Queue_Size"] = df["Queue_Size"].astype(int)
df["Replica_Count"] = df["Replica_Count"].astype(int)

# Plot
plt.figure(figsize=(12, 6))
plt.plot(df["Timestamp"], df["P99_Latency"], label="P99 Latency (s)")
plt.plot(df["Timestamp"], df["Queue_Size"], label="Queue Size")
plt.plot(df["Timestamp"], df["Replica_Count"], label="Replica Count")
plt.axhline(y=0.5, color='r', linestyle='--', label="Target Latency (0.5s)")
plt.title("Autoscaler Time-Series Performance")
plt.xlabel("Time")
plt.ylabel("Value")
plt.legend()
plt.grid(True)
plt.tight_layout()
plt.xticks(rotation=45)
plt.savefig(OUTPUT_PLOT)
plt.close()

# Print summary
start = df.iloc[0]
end = df.iloc[-1]
print("=== Autoscaler Summary ===")
print(f"Start P99 Latency: {start['P99_Latency']:.3f} s")
print(f"End P99 Latency:   {end['P99_Latency']:.3f} s")
print(f"Start Replicas:    {start['Replica_Count']}")
print(f"End Replicas:      {end['Replica_Count']}")
print(f"Plot saved to:     {OUTPUT_PLOT}")
