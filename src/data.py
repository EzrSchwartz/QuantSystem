import yfinance as yf
import pandas as pd
import numpy as np
from scipy.stats import zscore
from tqdm import tqdm
import time
import asyncio
import aiohttp
import os
from typing import Dict, List

#######################
# MASTER VARIABLES
#######################

# Target Sectors for Analysis
SECTORS = [
    "basic-materials",
    "communication-services",
    "consumer-cyclical",
    "consumer-defensive",
    "energy",
    "financial-services",
    "healthcare",
    "industrials",
    "real-estate",
    "technology",
    "utilities"
]

# Independent Variables (X) for Regression

# Category 1: Risk Metrics - Measures of company's risk profile
X1_RISK_METRICS = {
    "Beta": "beta",                          # Market sensitivity
    "Volatility": "regularMarketVolume",     # Trading volume volatility
    "DebtToEquity": "debtToEquity"          # Leverage ratio
}

# Category 2: Growth Metrics - Measures of company's growth potential
X2_GROWTH_METRICS = {
    "RevenueGrowth": "revenueGrowth",       # Top-line growth
    "EarningsGrowth": "earningsGrowth",      # Bottom-line growth
    "ProfitMargins": "profitMargins"         # Profitability
}

# Category 3: Quality Metrics - Measures of company's operational efficiency
X3_QUALITY_METRICS = {
    "ROE": "returnOnEquity",                # Return on Equity
    "ROA": "returnOnAssets",                # Return on Assets
    "OperatingMargin": "operatingMargins"   # Operating efficiency
}

# Dependent Variable (Y) for Regression
Y_VALUATION_METRIC = {
    "PE": "trailingPE"  # Trailing P/E ratio as valuation metric
    # Can be modified to use different valuation metrics:
    # "PB": "priceToBook"
    # "PS": "priceToSales"
    # "PFCF": "priceToFreeCashflow"
}

# Combine all metrics for data collection
ALL_METRICS = {
    **X1_RISK_METRICS,
    **X2_GROWTH_METRICS,
    **X3_QUALITY_METRICS,
    **Y_VALUATION_METRIC
}

# Create metrics dictionary for easy access
METRICS = {
    "x1_risk_metrics": X1_RISK_METRICS,        # X1 variable
    "x2_growth_metrics": X2_GROWTH_METRICS,    # X2 variable
    "x3_quality_metrics": X3_QUALITY_METRICS,  # X3 variable
    "y_valuation_metric": Y_VALUATION_METRIC,  # Y variable
    "all_metrics": ALL_METRICS
}

# API Settings
MAX_CONCURRENT_REQUESTS = 2
REQUEST_DELAY = 0.5  # seconds
INDUSTRY_DELAY = 1.0  # seconds

#######################
# CODE
#######################

# Semaphore for limiting concurrent requests
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

async def get_metric_async(session: aiohttp.ClientSession, ticker: str, metric_name: str) -> float:
    """Get a specific metric for a ticker asynchronously."""
    async with SEMAPHORE:  # Limit concurrent requests
        try:
            await asyncio.sleep(REQUEST_DELAY)  # Rate limiting
            stock = yf.Ticker(ticker)
            value = stock.info.get(metric_name)
            return value if value is not None else float("nan")
        except Exception as e:
            print(f"Error fetching {metric_name} for {ticker}: {e}")
            return float("nan")

async def get_company_metrics_async(session: aiohttp.ClientSession, company: str, 
                                 all_metrics: Dict[str, str]) -> Dict:
    """Get all metrics for a single company asynchronously."""
    company_data = {"Ticker": company}
    
    # Create tasks for all metrics
    tasks = [
        get_metric_async(session, company, yf_metric)
        for metric_name, yf_metric in all_metrics.items()
    ]
    
    # Wait for all metrics to be fetched
    results = await asyncio.gather(*tasks)
    
    # Add results to company data
    for (metric_name, _), value in zip(all_metrics.items(), results):
        company_data[metric_name] = value
    
    return company_data

async def process_companies_async(companies: List[str], all_metrics: Dict[str, str]) -> List[Dict]:
    """Process a batch of companies asynchronously."""
    async with aiohttp.ClientSession() as session:
        tasks = []
        for company in companies:
            task = get_company_metrics_async(session, company, all_metrics)
            tasks.append(task)
        
        # Use tqdm to show progress
        company_data_list = []
        for future in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Processing companies"):
            try:
                result = await future
                company_data_list.append(result)
            except Exception as e:
                print(f"Error processing company: {e}")
    
    return company_data_list

async def get_sector_companies(sector: str) -> List[str]:
    """Get companies for a given sector using yfinance."""
    try:
        # Use yfinance to get sector companies
        sector_obj = yf.Sector(sector)
        return list(sector_obj.top_companies.index)
    except Exception as e:
        print(f"Error getting companies for sector {sector}: {e}")
        return []

async def process_sector_async(sector: str, metrics: Dict[str, Dict[str, str]]) -> pd.DataFrame:
    """Process a single sector asynchronously."""
    try:
        companies = await get_sector_companies(sector)
        
        if not companies:
            print(f"No companies found for sector {sector}")
            return None
            
        # Get raw data
        company_data_list = await process_companies_async(companies, metrics["all_metrics"])
        
        # Convert to DataFrame
        df = pd.DataFrame(company_data_list)
        
        if df is None or df.empty:
            print(f"No data available for sector {sector}")
            return None
            
        # Add sector column before processing
        df["Sector"] = sector
        
        # Process data
        df = await process_data(df, metrics)
        return df
    except Exception as e:
        print(f"Error processing sector {sector}: {e}")
        return None

async def process_data(df: pd.DataFrame, metrics: Dict[str, Dict[str, str]]) -> pd.DataFrame:
    """Process the collected data by calculating z-scores and composite scores"""
    if df.empty:
        print("No data was collected successfully.")
        return None
    
    # Calculate z-scores for each metric group
    for metric_group in ["x1_risk_metrics", "x2_growth_metrics", "x3_quality_metrics"]:
        for metric_name in metrics[metric_group].keys():
            if metric_name in df.columns:
                df.loc[:, f"{metric_name}_ZScore"] = zscore(df[metric_name].fillna(df[metric_name].mean()), 
                                                   nan_policy='omit')
    
    # Calculate PE Z-score separately (our target variable)
    if "PE" in df.columns:
        # Remove companies with no P/E values
        df = df.dropna(subset=["PE"]).copy()  # Create explicit copy
        # Calculate z-score only for remaining companies
        df.loc[:, "PE_ZScore"] = zscore(df["PE"], nan_policy='omit')
    
    # Calculate composite scores for each category
    df.loc[:, "Risk_Score"] = df[[f"{metric}_ZScore" for metric in X1_RISK_METRICS.keys() 
                          if f"{metric}_ZScore" in df.columns]].mean(axis=1)
    df.loc[:, "Growth_Score"] = df[[f"{metric}_ZScore" for metric in X2_GROWTH_METRICS.keys() 
                            if f"{metric}_ZScore" in df.columns]].mean(axis=1)
    df.loc[:, "Quality_Score"] = df[[f"{metric}_ZScore" for metric in X3_QUALITY_METRICS.keys() 
                             if f"{metric}_ZScore" in df.columns]].mean(axis=1)
    
    # Filter out data points with extreme z-scores (>3 standard deviations)
    mask = (abs(df["Risk_Score"]) <= 2.5) & \
           (abs(df["Growth_Score"]) <= 2.5) & \
           (abs(df["Quality_Score"]) <= 2.5) & \
           (abs(df["PE_ZScore"]) <= 2.5)  
    df = df[mask]
    
    # Keep only essential columns
    columns_to_keep = ["Sector", "Ticker", "Risk_Score", "Growth_Score", "Quality_Score"]
    if "PE" in df.columns:
        columns_to_keep.extend(["PE", "PE_ZScore"])
    
    df = df[columns_to_keep]
    
    return df

async def analyze_sectors_async(sectors: List[str] = SECTORS) -> pd.DataFrame:
    """Analyze multiple sectors with controlled concurrency."""
    all_data = []
    
    for sector in tqdm(sectors, desc="Processing sectors"):
        await asyncio.sleep(INDUSTRY_DELAY)  # Rate limiting between sectors
        sector_data = await process_sector_async(sector, METRICS)
        if sector_data is not None:
            all_data.append(sector_data)
    
    if not all_data:
        print("No data was collected successfully.")
        return None
    
    # Combine all sector data
    combined_data = pd.concat(all_data, ignore_index=True)
    
    # Save to CSV
    combined_data.to_csv("sector_analysis.csv", index=False)
    return combined_data

if __name__ == "__main__":
    # Run the async analysis
    results = asyncio.run(analyze_sectors_async())
