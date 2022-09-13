from pathlib import Path

numApps = 3  # number of categories of Apps with different delay budgets
fileSize = 1 << 20  # file size in bits
delayBudgets = [.05, .15, .30]  # in seconds
appNames = ['Delay Stringent', 'Delay Sensitive', 'Delay Tolerant']
# peakSumRate = 1 << 30  # maximum downlink sum rate in bps
dpiSampleRate = 1/800  # probability of inspecting a traffic session in DPI data
profilesPath = Path(__file__).parent.parent/'data/cluster_traffic_profiles.csv'
