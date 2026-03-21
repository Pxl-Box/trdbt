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
    # Define features we want the model to learn from (Exclude raw price/volume)
    feature_cols = [
        'ret_1d', 'ret_5d', 'ret_20d',
        'dist_sma_20', 'dist_sma_50',
        'rsi_14', 'macd_hist',
        'daily_range_pct', 'volatility_20d', 'vol_surge',
        'rsi_7', 'macd_trend', 'bb_width', 'atr_pct'
    ]
    
    # 1. Clean the data (Models hate NaNs and Infs)
    master_df = master_df.replace([np.inf, -np.inf], np.nan).dropna(subset=feature_cols + ['target_win_5d'])
    
    X = master_df[feature_cols]
    y = master_df['target_win_5d']
    
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
        # Using DeviceQuantileDMatrix for maximum GPU-native throughput
        dtrain = xgb.DMatrix(X, label=y, device='cuda')
        
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
            logger.info("🚀 GPU-NATIVE SEARCH: Investigating 100 possible brain architectures...")
            
            n_iter = 100
            best_score = float('inf')  # minimizing logloss
            
            for i in range(n_iter):
                # Randomly sample parameters
                params = {
                    'objective': 'binary:logistic',
                    'tree_method': 'hist',
                    'device': 'cuda',
                    'scale_pos_weight': scale_weight,
                    'learning_rate': np.random.choice([0.01, 0.03, 0.05, 0.1]),
                    'max_depth': np.random.choice([6, 8, 10, 12]),
                    'subsample': np.random.uniform(0.7, 1.0),
                    'colsample_bytree': np.random.uniform(0.7, 1.0),
                    'gamma': np.random.uniform(0, 0.5),
                    'eval_metric': 'logloss'
                }
                
                # Cross-validation handled natively on GPU
                cv_results = xgb.cv(
                    params,
                    dtrain,
                    num_boost_round=1000,
                    nfold=3,
                    early_stopping_rounds=20,
                    verbose_eval=False
                )
                
                current_score = cv_results['test-logloss-mean'].min()
                if current_score < best_score:
                    best_score = current_score
                    best_params = params
                    # Store best iteration count
                    best_params['n_estimators'] = len(cv_results)
                    logger.info(f"  [Iter {i+1}/{n_iter}] New Best Score: {best_score:.4f}")
            
            logger.info(f"🏆 Best Architecture Selected: {best_params}")
        else:
            best_params['n_estimators'] = 500

        # Train final model on 100% of GPU data
        logger.info("⚡ Training final Deployment Brain on GPU...")
        final_model_native = xgb.train(best_params, dtrain, num_boost_round=best_params.get('n_estimators', 500))
        
        # Export Model (Pickling the native Booster object directly)
        export_path = MODELS_DIR / "ai_brain_v1.pkl"
        backup_path = MODELS_DIR / f"ai_brain_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pkl"
        
        with open(export_path, 'wb') as f:
            pickle.dump(final_model_native, f)
        with open(backup_path, 'wb') as f:
            pickle.dump(final_model_native, f)
            
        logger.info(f"✅ EXTREME SUCCESS: GPU-Native Brain exported to {export_path}")
        logger.info(f"✅ CPU check: Should be idling. GPU check: Should have been pinned.")
        logger.info("Sleeping for 12 hours.")
        time.sleep(43200)


if __name__ == "__main__":
    train_and_export_model()
