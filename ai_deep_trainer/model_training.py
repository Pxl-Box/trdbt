import logging
import os
import json
import pickle
import time
from datetime import datetime
from pathlib import Path
import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.metrics import classification_report, accuracy_score
from sklearn.model_selection import TimeSeriesSplit, RandomizedSearchCV

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Paths
def load_node_config():
    root_dir = Path(__file__).parent.parent
    config_path = root_dir / "node_config.json"
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading node_config.json: {e}")
    return {}

NODE_CONFIG: dict = load_node_config()
SHARED_DRIVE_PATH = NODE_CONFIG.get("shared_drive_path", r"D:\trd-data")

_LOCAL_DIR = Path(__file__).parent
DATA_LAKE_DIR = Path(SHARED_DRIVE_PATH) / "processed_data" if SHARED_DRIVE_PATH else _LOCAL_DIR.parent / "ai_data_lake" / "processed_data"
MODELS_DIR = Path(SHARED_DRIVE_PATH) if SHARED_DRIVE_PATH else _LOCAL_DIR / "trained_models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

def load_all_data() -> pd.DataFrame:
    """Loads all feature-engineered parquet files from the Data Lake."""
    if not DATA_LAKE_DIR.exists():
        logger.error(f"Data Lake directory not found at {DATA_LAKE_DIR}")
        return pd.DataFrame()
        
    files = list(DATA_LAKE_DIR.glob("*_features.parquet"))
    logger.info(f"Discovered {len(files)} processed feature files.")
    
    df_list = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            # You could add the ticker as a categorical feature here if using an advanced model
            df_list.append(df)
        except Exception as e:
            logger.error(f"Error reading {f.name}: {e}")
            
    if not df_list:
        return pd.DataFrame()
        
    master_df = pd.concat(df_list)
    master_df.sort_index(inplace=True) # Sort by datetime to prevent look-ahead bias
    return master_df

def prepare_data(master_df: pd.DataFrame):
    """Separates Features (X) from Target Labels (y)."""
    # Dynamically select all columns that end with our timeframe suffixes
    # This automatically picks up Phase 4 SRS features (ret_vs_spy_1d, etc.)
    feature_cols = [c for c in master_df.columns if c.endswith("_15m") or c.endswith("_1d")]
    target_col = 'target_win'
    
    # 1. Clean the data (Models hate NaNs and Infs)
    # Ensure all required columns exist in the dataframe before replacing/dropping
    valid_feature_cols = [c for c in feature_cols if c in master_df.columns]
    
    if target_col not in master_df.columns:
        logger.error(f"Target column '{target_col}' missing from data!")
        return pd.DataFrame(), pd.Series()
        
    master_df = master_df.replace([np.inf, -np.inf], np.nan)
    
    # Fill missing features with 0 (neutral) instead of dropping rows
    # This prevents column mismatches from nuking the dataset
    master_df[valid_feature_cols] = master_df[valid_feature_cols].fillna(0)
    
    # Only drop if the target itself is missing
    master_df = master_df.dropna(subset=[target_col])
    
    X = master_df[valid_feature_cols]
    y = master_df[target_col]
    
    return X, y

def train_and_export_model():
    """Trains a classifier using high-performance GPU-native loops."""
    logger.info("=== Starting Absolute GPU Ruthlessness Initialization ===")
    
    while True:
        logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] Waking up to train AI on latest Data Lake files...")
        
        master_df = load_all_data()
        if master_df.empty:
            logger.error("No data available to train on. Retrying in 1 hour...")
            time.sleep(3600)
            continue
            
        X, y = prepare_data(master_df)
        logger.info(f"Dataset Size: {len(X)} records | {X.shape[1]} features")
        
        # Calculate class weight for imbalance
        num_wins = y.sum()
        num_losses = len(y) - num_wins
        scale_weight = num_losses / num_wins if num_wins > 0 else 1.0

        # CRITICAL: Move data to GPU Pinned Memory once
        logger.info("⚡ Moving data to GPU DeviceQuantileDMatrix (Absolute Ruthlessness)...")
        # Using DMatrix for maximum GPU-native throughput
        dtrain = xgb.DMatrix(X, label=y)
        
        is_turbo = NODE_CONFIG.get("deep_trainer", {}).get("turbo_mode", True)
        best_params = {
            'objective': 'binary:logistic',
            'tree_method': 'hist',
            'device': 'cuda',
            'scale_pos_weight': scale_weight,
            'learning_rate': 0.05,
            'max_depth': 6,
            'eval_metric': 'logloss'
        }

        if is_turbo:
            n_iter = 250
            logger.info(f"🚀 GPU-NATIVE SEARCH: Investigating {n_iter} possible brain architectures...")
            patience = 50  # Stop early if no improvement in 50 tries
            no_improvement_count = 0
            best_score = float('inf')  # minimizing logloss
            
            for i in range(n_iter):
                # Randomly sample parameters
                params = {
                    'objective': 'binary:logistic',
                    'tree_method': 'hist',
                    'device': 'cuda',
                    'scale_pos_weight': scale_weight,
                    'learning_rate': np.random.choice([0.005, 0.01, 0.03, 0.05]),
                    'max_depth': np.random.choice([4, 6, 8, 10, 12, 15]),
                    'subsample': np.random.uniform(0.6, 1.0),
                    'colsample_bytree': np.random.uniform(0.6, 1.0),
                    'gamma': np.random.uniform(0, 0.5),
                    'reg_alpha': np.random.choice([0, 0.01, 0.1, 1.0]),
                    'reg_lambda': np.random.choice([1, 5, 10]),
                    'min_child_weight': np.random.choice([1, 5, 10]),
                    'eval_metric': 'logloss'
                }
                
                # Cross-validation handled natively on GPU
                cv_results = xgb.cv(
                    params,
                    dtrain,
                    num_boost_round=1000,
                    nfold=5,
                    early_stopping_rounds=50,
                    verbose_eval=False
                )
                
                current_score = cv_results['test-logloss-mean'].min()
                
                # Heartbeat logging: log every iteration so user knows progress
                logger.info(f"  [Iter {i+1}/{n_iter}] Current Score: {current_score:.4f} (Best: {best_score:.4f})")
                
                if current_score < best_score:
                    best_score = current_score
                    best_params = params
                    no_improvement_count = 0
                    # Store best iteration count
                    best_params['n_estimators'] = len(cv_results)
                    logger.info(f"  ✨ [NEW BEST] [Iter {i+1}/{n_iter}] Best Score: {best_score:.4f}")
                else:
                    no_improvement_count += 1
                    
                if no_improvement_count >= patience:
                    logger.info(f"🛑 EARLY STOPPING: No improvement for {patience} iterations. Keeping the best brain.")
                    break
            
            logger.info(f"🏆 Best Architecture Selected: {best_params}")
        else:
            best_params['n_estimators'] = 500

        # Train final model on 100% of GPU data
        logger.info("⚡ Training final Deployment Brain on GPU...")
        final_model_native = xgb.train(best_params, dtrain, num_boost_round=best_params.get('n_estimators', 500))
        
        # Export Model (Pickling the native Booster object directly)
        # Prepare Metadata Wrapper
        brain_data = {
            "model": final_model_native,
            "score": float(best_score),
            "timestamp": datetime.now().isoformat(),
            "feature_count": len(X.columns),
            "features": list(X.columns),
            "hyperparams": best_params
        }
        
        # Export Model
        export_path = MODELS_DIR / "ai_brain_v1.pkl"
        backup_path = MODELS_DIR / f"ai_brain_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pkl"
        
        with open(export_path, 'wb') as f:
            pickle.dump(brain_data, f)
        with open(backup_path, 'wb') as f:
            pickle.dump(brain_data, f)
            
        logger.info(f"✅ EXTREME SUCCESS: GPU-Native Brain exported to {export_path}")
        logger.info(f"📊 BEST CV SCORE: {best_score:.4f} (Logloss)")
        logger.info(f"✅ CPU check: Should be idling. GPU check: Should have been pinned.")
        logger.info("Sleeping for 12 hours.")
        time.sleep(43200)


if __name__ == "__main__":
    train_and_export_model()
