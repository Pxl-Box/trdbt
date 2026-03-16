import logging
import os
import pickle
import time
from datetime import datetime
from pathlib import Path
import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.metrics import classification_report, accuracy_score
from sklearn.model_selection import TimeSeriesSplit

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Paths
# Configure this to point to your 30TB Shared Network Drive
SHARED_DATA_LAKE_DIR = None
SHARED_MODELS_DIR = None

_LOCAL_DIR = Path(__file__).parent
DATA_LAKE_DIR = Path(SHARED_DATA_LAKE_DIR) / "processed_data" if SHARED_DATA_LAKE_DIR else _LOCAL_DIR.parent / "ai_data_lake" / "processed_data"
MODELS_DIR = Path(SHARED_MODELS_DIR) if SHARED_MODELS_DIR else _LOCAL_DIR / "trained_models"
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
        'daily_range_pct', 'volatility_20d', 'vol_surge'
    ]
    
    # 1. Clean the data (Models hate NaNs and Infs)
    master_df = master_df.replace([np.inf, -np.inf], np.nan).dropna(subset=feature_cols + ['target_win_5d'])
    
    X = master_df[feature_cols]
    y = master_df['target_win_5d']
    
    return X, y

def train_and_export_model():
    """Trains a classifier using Time-Series Walk-Forward validation and exports it."""
    logger.info("=== Starting Deep Trainer Initialization ===")
    
    # Continuous Training Loop
    while True:
        logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] Waking up to train AI on latest Data Lake files...")
        
        master_df = load_all_data()
        if master_df.empty:
            logger.error("No data available to train on. Retrying in 1 hour...")
            time.sleep(3600)
            continue
            
        logger.info(f"Loaded a Master Dataset of {len(master_df)} total historical records.")
        
        X, y = prepare_data(master_df)
        logger.info(f"Features: {X.shape[1]}, Records: {X.shape[0]}")
        logger.info(f"Class Balance (Target = 1 Win): {y.mean() * 100:.2f}% of trades.")
        
        # CRITICAL: Time Series Split to prevent Future Leakage (Look-Ahead Bias)
        tscv = TimeSeriesSplit(n_splits=3)
        
        # XGBoost setup for GPU Acceleration
        num_wins = y.sum()
        num_losses = len(y) - num_wins
        scale_weight = num_losses / num_wins if num_wins > 0 else 1.0
        
        logger.info(f"Initializing XGBClassifier with GPU Acceleration (scale_pos_weight={scale_weight:.2f})...")
        
        baseline_params = {
            'n_estimators': 200,
            'max_depth': 5,
            'learning_rate': 0.05,
            'random_state': 42,
            'scale_pos_weight': scale_weight,
            'tree_method': 'hist', # Required for GPU acceleration
            'device': 'cuda',      # Instructs XGBoost to use the Nvidia GPU
            'n_jobs': -1           # Max CPU threads for data ingestion/prep
        }
        
        model = xgb.XGBClassifier(**baseline_params)
        
        # Walk-Forward Validation
        fold = 1
        accuracies = []
        
        logger.info("Starting Walk-Forward Validation...")
        for train_index, test_index in tscv.split(X):
            X_train, X_test = X.iloc[train_index], X.iloc[test_index]
            y_train, y_test = y.iloc[train_index], y.iloc[test_index]
            
            model.fit(X_train, y_train)
            preds = model.predict(X_test)
            
            acc = accuracy_score(y_test, preds)
            accuracies.append(acc)
            logger.info(f"Fold {fold} Accuracy: {acc * 100:.2f}%")
            fold += 1
            
        logger.info(f"Average CV Accuracy: {np.mean(accuracies) * 100:.2f}%")
        
        # Train the final model on 100% of the data to deploy
        logger.info("Training final deployable 'Brain' on ALL historical data using GPU...")
        
        final_params = baseline_params.copy()
        final_params['n_estimators'] = 500 # Train harder on the full dataset
        final_params['max_depth'] = 6
        
        final_model = xgb.XGBClassifier(**final_params)
        final_model.fit(X, y)
        
        # Export Model (Overwrite the active model file, and maybe save a timestamped backup)
        export_path = MODELS_DIR / "ai_brain_v1.pkl"
        backup_path = MODELS_DIR / f"ai_brain_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pkl"
        
        with open(export_path, 'wb') as f:
            pickle.dump(final_model, f)
            
        with open(backup_path, 'wb') as f:
            pickle.dump(final_model, f)
            
        logger.info(f"✅ Successfully exported GPU-trained Brain to {export_path}")
        logger.info(f"✅ Backup saved to {backup_path.name}")
        logger.info("Going to sleep. Will retrain in 12 hours.")
        
        # Sleep for 12 hours (43200 seconds) before retraining
        time.sleep(43200)

if __name__ == "__main__":
    train_and_export_model()
