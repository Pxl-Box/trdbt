import pickle
import xgboost as xgb
import os

path = r"C:\Users\Conor\Documents\GitHub\trdbt\trained_models\ai_brain_v1.pkl"

print(f"Checking brain at: {path}")
if not os.path.exists(path):
    print("ERROR: File not found.")
    exit(1)

try:
    with open(path, 'rb') as f:
        model = pickle.load(f)
    
    print(f"Object Type: {type(model)}")
    
    # Check if it's a dict (metadata wrapper)
    if isinstance(model, dict):
        print(f"--- Brain Metadata Found ---")
        print(f"SCORE (Logloss): {model.get('score', 'N/A')}")
        print(f"TRAINED AT: {model.get('timestamp', 'N/A')}")
        print(f"FEATURE COUNT: {model.get('feature_count', 'N/A')}")
        if 'hyperparams' in model:
            print(f"HYPERPARAMS: {model['hyperparams']}")
        
        # Extract model and continue analysis
        if 'model' in model:
            model = model['model']
            print(f"--- Internal Model Loaded ---")

    # Attempt to extract feature names
    features = []
    if hasattr(model, 'feature_names_in_'):
        features = list(model.feature_names_in_)
    elif hasattr(model, 'get_booster'):
        features = model.get_booster().feature_names
    elif hasattr(model, 'feature_names'):
        features = model.feature_names
    elif isinstance(model, xgb.Booster):
        features = model.feature_names
        
    if features:
        print(f"SUCCESS: Brain Loaded.")
        print(f"FEATURE COUNT: {len(features)}")
        print(f"FEATURES: {features}")
        
        # Verify 38 count
        if len(features) == 38:
            print("VERIFICATION: PERFECT 38-FEATURE MATCH! ✅")
        else:
            print(f"VERIFICATION: MISMATCH ({len(features)} vs 38) ❌")
    else:
        print("WARNING: Could not automatically extract feature names. Model might be raw booster without names.")
        # Try to infer count from booster if possible
        if isinstance(model, xgb.Booster):
            # This is hard without data, but we can check the length of model.predict on a dummy
            print("Detected raw XGBoost Booster.")

except Exception as e:
    print(f"ERROR: Failed to load brain: {e}")
    import traceback
    traceback.print_exc()
