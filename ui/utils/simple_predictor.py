"""
Simple predictor for sales forecasting
"""

import pandas as pd
import numpy as np
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)


class SimplePredictor:
    """Simple predictor that works with SimpleModelLoader"""
    
    def __init__(self, model_loader):
        self.model_loader = model_loader
        
    def prepare_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Prepare features for prediction"""
        # Ensure date column is datetime
        df = df.copy()
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            
            # Extract time features
            df['year'] = df['date'].dt.year
            df['month'] = df['date'].dt.month
            df['day'] = df['date'].dt.day
            df['dayofweek'] = df['date'].dt.dayofweek
            df['quarter'] = df['date'].dt.quarter
            df['weekofyear'] = df['date'].dt.isocalendar().week
            df['is_weekend'] = df['dayofweek'].isin([5, 6]).astype(int)
            
            # Add cyclical features
            df['month_sin'] = np.sin(2 * np.pi * df['month'] / 12)
            df['month_cos'] = np.cos(2 * np.pi * df['month'] / 12)
            df['day_sin'] = np.sin(2 * np.pi * df['day'] / 31)
            df['day_cos'] = np.cos(2 * np.pi * df['day'] / 31)
            df['dayofweek_sin'] = np.sin(2 * np.pi * df['dayofweek'] / 7)
            df['dayofweek_cos'] = np.cos(2 * np.pi * df['dayofweek'] / 7)
            
        # Add lag features if we have sales data
        if 'sales' in df.columns:
            # Multiple lag features
            for lag in [1, 2, 3, 7, 14, 21, 30]:
                df[f'sales_lag_{lag}'] = df['sales'].shift(lag)
            
            # Rolling statistics for different windows
            for window in [3, 7, 14, 21, 30]:
                df[f'sales_rolling_{window}_mean'] = df['sales'].rolling(window).mean()
                df[f'sales_rolling_{window}_std'] = df['sales'].rolling(window).std()
                df[f'sales_rolling_{window}_min'] = df['sales'].rolling(window).min()
                df[f'sales_rolling_{window}_max'] = df['sales'].rolling(window).max()
                df[f'sales_rolling_{window}_median'] = df['sales'].rolling(window).median()
            
            # Fill NaN values with appropriate defaults
            sales_mean = df['sales'].mean()
            for col in df.columns:
                if 'sales_lag' in col or 'sales_rolling' in col:
                    if 'std' in col:
                        df[col] = df[col].fillna(0)
                    else:
                        df[col] = df[col].fillna(sales_mean)
        
        # Add default values for features that might be missing
        if 'quantity_sold' not in df.columns:
            df['quantity_sold'] = 100  # Default quantity
        if 'profit' not in df.columns:
            df['profit'] = 1000  # Default profit
        if 'has_promotion' not in df.columns:
            df['has_promotion'] = 0  # No promotion by default
        if 'customer_traffic' not in df.columns:
            df['customer_traffic'] = 500  # Default traffic
        if 'is_holiday' not in df.columns:
            df['is_holiday'] = 0  # Not holiday by default
            
        return df
    
    def predict(self, input_data: pd.DataFrame, model_type: str = 'ensemble', 
                forecast_days: int = 30) -> Dict[str, Any]:
        """Make predictions"""
        try:
            if not self.model_loader.loaded:
                return {
                    'success': False,
                    'error': 'Models not loaded'
                }
            
            # Prepare historical data
            historical_df = self.prepare_features(input_data)
            historical_df['date'] = pd.to_datetime(historical_df['date'])
            historical_df = historical_df.sort_values('date').reset_index(drop=True)
            
            # Create future dates
            last_date = pd.to_datetime(input_data['date']).max()
            future_dates = pd.date_range(
                start=last_date + pd.Timedelta(days=1),
                periods=forecast_days,
                freq='D'
            )
            
            # Build robust defaults from historical data for features the model expects.
            numeric_defaults: Dict[str, float] = {}
            if not historical_df.empty:
                numeric_cols = historical_df.select_dtypes(include=[np.number]).columns
                for col in numeric_cols:
                    if col != 'sales':
                        numeric_defaults[col] = float(historical_df[col].mean())

            store_value = (
                input_data['store_id'].iloc[-1]
                if 'store_id' in input_data.columns and len(input_data) > 0
                else 'store_001'
            )

            sales_history = historical_df['sales'].dropna().astype(float).tolist()
            if not sales_history:
                return {'success': False, 'error': 'Input data must include non-empty sales history'}

            def _encode_store_id(value):
                if self.model_loader.encoders and 'store_id' in self.model_loader.encoders:
                    try:
                        encoder = self.model_loader.encoders['store_id']
                        known = list(encoder.classes_)
                        safe_value = value if value in known else known[0]
                        return int(encoder.transform([safe_value])[0])
                    except Exception as e:
                        logger.warning(f"Error encoding store_id: {e}")
                if isinstance(value, str) and 'store_' in value:
                    digits = ''.join(ch for ch in value if ch.isdigit())
                    return int(digits) if digits else 1
                try:
                    return int(value)
                except Exception:
                    return 1

            def _rolling(values, window, fn):
                if len(values) >= window:
                    arr = np.array(values[-window:], dtype=float)
                else:
                    arr = np.array(values, dtype=float)
                if arr.size == 0:
                    return 0.0
                return float(fn(arr))

            predictions_list = []
            for forecast_date in future_dates:
                row = {'date': forecast_date, 'store_id': store_value}
                row = self.prepare_features(pd.DataFrame([row])).iloc[0].to_dict()

                # Dynamic lag and rolling features based on latest observed/predicted values.
                for lag in [1, 2, 3, 7, 14, 21, 30]:
                    row[f'sales_lag_{lag}'] = (
                        sales_history[-lag] if len(sales_history) >= lag else float(np.mean(sales_history))
                    )

                for window in [3, 7, 14, 21, 30]:
                    row[f'sales_rolling_{window}_mean'] = _rolling(sales_history, window, np.mean)
                    row[f'sales_rolling_{window}_std'] = _rolling(sales_history, window, np.std)
                    row[f'sales_rolling_{window}_min'] = _rolling(sales_history, window, np.min)
                    row[f'sales_rolling_{window}_max'] = _rolling(sales_history, window, np.max)
                    row[f'sales_rolling_{window}_median'] = _rolling(sales_history, window, np.median)

                row['store_id'] = _encode_store_id(store_value)

                if self.model_loader.feature_cols:
                    feature_cols = self.model_loader.feature_cols
                else:
                    feature_cols = [
                        'year', 'month', 'day', 'dayofweek', 'quarter',
                        'is_weekend', 'sales_lag_1', 'sales_lag_7',
                        'sales_rolling_7_mean', 'sales_rolling_7_std'
                    ]

                for col in feature_cols:
                    if col not in row:
                        row[col] = numeric_defaults.get(col, 0.0)

                X = np.array([[row[col] for col in feature_cols]], dtype=float)

                # Scale features if scaler is available
                if self.model_loader.scalers and 'features' in self.model_loader.scalers:
                    try:
                        X = self.model_loader.scalers['features'].transform(X)
                    except Exception as e:
                        logger.warning(f"Could not apply feature scaling: {e}")

                pred = self.model_loader.predict(X, model_type=model_type)
                pred_value = float(np.array(pred).flatten()[0])
                predictions_list.append(pred_value)
                sales_history.append(pred_value)

            predictions = np.array(predictions_list, dtype=float)
            
            # Scale predictions back if scaler is available
            if self.model_loader.scalers and 'target' in self.model_loader.scalers:
                try:
                    predictions = self.model_loader.scalers['target'].inverse_transform(
                        predictions.reshape(-1, 1)
                    ).flatten()
                except:
                    logger.warning("Could not inverse transform predictions")
            
            # Create results dataframe
            results_df = pd.DataFrame({
                'date': future_dates,
                'predicted_sales': predictions,
                'lower_bound': predictions * 0.9,  # Simple 10% confidence interval
                'upper_bound': predictions * 1.1
            })
            
            # Calculate summary statistics
            summary = {
                'total_predicted_sales': predictions.sum(),
                'average_daily_sales': predictions.mean(),
                'max_daily_sales': predictions.max(),
                'min_daily_sales': predictions.min()
            }
            
            return {
                'success': True,
                'predictions': results_df,
                'summary': summary,
                'model_type': model_type
            }
            
        except Exception as e:
            logger.error(f"Prediction error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {
                'success': False,
                'error': str(e)
            }