import pickle
import xgboost as xgb
from pathlib import Path
import sys

def verify_brain(brain_path):
    print(f"--- Verifying Brain at: {brain_path} ---")
    if not Path(brain_path).exists():
        print(f"ERROR: File not found!")
        return

    try:
        with open(brain_path, 'rb') as f:
            brain = pickle.load(f)
        
        print(f"Type: {type(brain)}")
        
        if isinstance(brain, xgb.Booster):
            num_features = brain.num_features()
            feature_names = brain.feature_names
            print(f"Status: SUCCESS (Native Booster)")
            print(f"Feature Count: {num_features}")
            if feature_names:
                print(f"First 10 Features: {feature_names[:10]}")
            else:
                print("Note: No feature names stored (standard for native boosters).")
        else:
            print(f"Status: WARNING (Unexpected Type: {type(brain)})")
    except Exception as e:
        print(f"FAIL: {e}")

if __name__ == "__main__":
    verify_brain(r"D:\trd-data\ai_brain_v1.pkl")
