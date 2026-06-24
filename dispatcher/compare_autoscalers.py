import pandas as pd
import matplotlib.pyplot as plt
import sys

def load_log(path, label):
    df = pd.read_csv(path, parse_dates=["Timestamp"])
    df = df[df["P99_Latency"] != "N/A"].copy()
    df["P99_Latency"] = df["P99_Latency"].astype(float)
    df["Replica_Count"] = df["Replica_Count"].astype(int)
    df["label"] = label
    # Normalize time to seconds from start
    df["elapsed"] = (df["Timestamp"] - df["Timestamp"].iloc[0]).dt.total_seconds()
    return df

# Usage: python compare_autoscalers.py custom.csv hpa70.csv hpa90.csv
if len(sys.argv) != 4:
    print("Usage: python compare_autoscalers.py custom.csv hpa70.csv hpa90.csv")
    sys.exit(1)

custom = load_log(sys.argv[1], "Custom Autoscaler")
hpa70  = load_log(sys.argv[2], "HPA @ 70% CPU")
hpa90  = load_log(sys.argv[3], "HPA @ 90% CPU")

fig, axes = plt.subplots(2, 1, figsize=(14, 10))

# --- Plot 1: P99 Latency ---
for df in [custom, hpa70, hpa90]:
    axes[0].plot(df["elapsed"], df["P99_Latency"], label=df["label"].iloc[0])
axes[0].axhline(y=0.5, color="#9467BD", linestyle="--", label="Target (0.5s)")
axes[0].set_title("P99 Latency Comparison")
axes[0].set_xlabel("Elapsed Time (s)")
axes[0].set_ylabel("P99 Latency (s)")
axes[0].legend()
axes[0].grid(True)

# --- Plot 2: Replica Count (proxy for CPU cores used) ---
for df in [custom, hpa70, hpa90]:
    axes[1].plot(df["elapsed"], df["Replica_Count"], label=df["label"].iloc[0])
axes[1].set_title("Replica Count Over Time (CPU Cores)")
axes[1].set_xlabel("Elapsed Time (s)")
axes[1].set_ylabel("Replica Count")
axes[1].legend()
axes[1].grid(True)

plt.tight_layout()
plt.savefig("comparison_plot.png", dpi=150)
plt.close()
print("[✓] Saved comparison_plot.png")

# Summary table
print("\n=== Summary ===")
print(f"{'Autoscaler':<22} {'Avg P99 (s)':<15} {'Max P99 (s)':<15} {'Avg Replicas'}")
print("-" * 65)
for df in [custom, hpa70, hpa90]:
    label = df["label"].iloc[0]
    print(f"{label:<22} {df['P99_Latency'].mean():<15.3f} {df['P99_Latency'].max():<15.3f} {df['Replica_Count'].mean():.2f}")