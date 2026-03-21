import os
import logging
import pickle
import pandas as pd
import numpy as np
import xgboost as xgb

logger = logging.getLogger(__name__)

class QuantInference:
    """
    Node 3 (Execution Node) Inference Engine.
    Responsible for loading the 'Brain' (.pkl model) sent from Node 2 (Deep Trainer)
    and calculating optimal bet sizing using the Kelly Criterion.
    """
    def __init__(self, model_path="trained_models/ai_brain_v1.pkl"):
        self.model_path = model_path
        self.model = None
        self._load_model()

    def _load_model(self):
        """Attempts to load the pre-trained Machine Learning model."""
        if not os.path.exists(self.model_path):
            logger.info(f"[Quant] No AI model found at {self.model_path}. Running purely on standard math.")
            return

        try:
            with open(self.model_path, "rb") as f:
                self.model = pickle.load(f)
            logger.info(f"[Quant] Successfully loaded AI Brain from {self.model_path}")
        except Exception as e:
            logger.error(f"[Quant] Failed to load AI model: {e}")

    def is_ai_active(self) -> bool:
        """Returns True if the ML model is successfully loaded and ready."""
        return self.model is not None

    def get_win_probability(self, features_df: pd.DataFrame) -> float:
        """
        Feeds the real-time calculated features into the ML model to get a Win Probability.
        If no model is loaded, returns a base 0.50 (coin flip).
        """
        if not self.is_ai_active():
            return 0.50

        try:
            if hasattr(self.model, 'predict_proba'):
                # Scikit-learn wrapper (XGBClassifier)
                probabilities = self.model.predict_proba(features_df)
                win_prob = float(probabilities[-1][1])
            else:
                # Native XGBoost Booster or other
                # Use DMatrix for inference (fast)
                dmat = xgb.DMatrix(features_df)
                preds = self.model.predict(dmat)
                win_prob = float(preds[-1])
            
            return win_prob
        except Exception as e:
            logger.warning(f"[Quant] Inference failed: {e}. Defaulting to 50% probability.")
            return 0.50

    def calculate_kelly_fraction(self, win_prob: float, reward_risk_ratio: float, 
                                 fractional_kelly: float = 0.5, max_allocation: float = 0.05) -> float:
        """
        Calculates the optimal capital allocation using the Kelly Criterion.
        
        Formula: f* = p - (1 - p) / b
          p = probability of winning (win_prob)
          b = ratio of average win to average loss (reward_risk_ratio)
          
        Args:
            win_prob: The AI's predicted chance of winning (e.g. 0.65 for 65%)
            reward_risk_ratio: The TP% divided by SL% (e.g., if TP is 3% and SL is 1.5%, b=2)
            fractional_kelly: A safety divisor (e.g., 0.5 for Half-Kelly, to reduce volatility)
            max_allocation: The absolute maximum % of capital to risk on one trade (e.g., 0.05 for 5%)
            
        Returns:
            Optimal % of free capital to wager on the trade (clamped between 0.0 and max_allocation)
        """
        if win_prob <= 0.0 or reward_risk_ratio <= 0.0:
            return 0.0  # Cannot compute Kelly

        loss_prob = 1.0 - win_prob
        
        # Kelly Formula
        f_star = win_prob - (loss_prob / reward_risk_ratio)
        
        # If mathematically negative edge, bet 0
        if f_star <= 0:
            return 0.0
            
        # Apply Fractional Kelly (Safety Factor)
        f_safe = f_star * fractional_kelly
        
        # Clamp to max_allocation to prevent blowing the account on one trade
        final_allocation = min(f_safe, max_allocation)
        
        return final_allocation
