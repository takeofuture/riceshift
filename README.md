# RiceShift

## NASA-Powered Rotation Advisor for California Rice Farms

**NASA Space Apps Challenge 2026 — Field Shift: Adapting Farms with NASA Data**  
**Team Hachi**

RiceShift is a data-driven decision-support prototype for California rice agriculture.  
It combines long-term crop-history data, NASA Earth observations, soil properties, and agricultural outcomes to explore which crop-rotation strategies may be more appropriate under different field and environmental conditions.

---

## NASA Space Apps Challenge 2026

RiceShift was developed for the **2026 NASA International Space Apps Challenge**, held on **November 14–15, 2026**.

Official event page:

https://www.spaceappschallenge.org/2026/

Official 2026 challenge list:

https://www.spaceappschallenge.org/2026/challenges/

### Challenge

**Field Shift: Adapting Farms with NASA Data**

The challenge asks participants to create a decision-support tool that uses NASA Earth observations together with local soil information, crop characteristics, and farmer priorities to help farmers explore crop-rotation strategies that can improve resilience to changing environmental conditions.

RiceShift focuses this challenge on **California rice farming in the Sacramento Valley**.

---

## Project concept

Rice farms face management decisions that depend on more than yield alone.

A field that performed well under one combination of water availability, winter moisture, soil properties, and historical management may not respond the same way under different environmental conditions.

RiceShift therefore asks three practical questions:

1. **What has happened?**  
   How have rice production and crop-rotation patterns changed over time?

2. **Under what environmental conditions?**  
   How do winter and rice-growing-season conditions differ?

3. **How are those conditions related to agricultural outcomes?**  
   What relationships can be observed between Earth-observation signals and rice yield?

The prototype then compares three candidate management strategies:

- **Continuous Rice**
- **Rice → Winter Vetch → Rice**
- **Rice → Winter Cover Crop Mix → Rice**

RiceShift is intended as a **decision-support and screening framework**, not as a causal agronomic model or replacement for field validation.

---

## Study area

The project focuses on the major rice-growing region of the **Sacramento Valley, California**.

Broad analysis bounding box:

```text
West   -123.20
South    38.00
East   -120.70
North    40.70
```

---

## Data sources

### NASA data

#### NASA MODIS

MODIS observations are used to characterize crop and surface conditions, including:

- vegetation indices such as NDVI and EVI
- LAI / FPAR
- land-surface temperature
- evapotranspiration / potential evapotranspiration
- surface reflectance
- water-related spectral indices

NASA Earthdata:

https://earthdata.nasa.gov/

NASA Earthdata Search:

https://search.earthdata.nasa.gov/

#### NASA SMAP

RiceShift uses **SMAP Enhanced L3 Radiometer Daily 9 km Soil Moisture** to describe seasonal soil-moisture conditions.

The project separates:

- **Rice season:** April–November
- **Winter cover-crop window:** November of the previous year through March

NASA SMAP mission:

https://smap.jpl.nasa.gov/

NASA Earthdata Login:

https://urs.earthdata.nasa.gov/

#### NASA POWER

The preparation pipeline also supports regional NASA POWER climate data for the Sacramento Valley.

NASA POWER:

https://power.larc.nasa.gov/

---

### USDA and other supporting data

#### USDA NASS Cropland Data Layer (CDL)

Used to reconstruct annual crop history and rice rotation patterns from **2007–2025**.

https://nassgeodata.gmu.edu/CropScape/

#### USDA NASS Quick Stats

Used for annual California rice outcomes including:

- planted acres
- harvested acres
- production
- yield

https://quickstats.nass.usda.gov/

Quick Stats API:

https://quickstats.nass.usda.gov/api/

#### USDA NRCS SSURGO

Used to characterize top-30-cm soil properties including:

- pH
- electrical conductivity
- sodium adsorption ratio
- saturated hydraulic conductivity
- soil texture
- organic matter
- restrictive layers
- soil taxonomy / capability information

USDA Web Soil Survey:

https://websoilsurvey.nrcs.usda.gov/

USDA Soil Data Access:

https://sdmdataaccess.sc.egov.usda.gov/

---

## Analysis periods

| Analysis | Period | Purpose |
|---|---:|---|
| Literature-aligned rotation window | 2007–2021 | Align with the historical rotation logic used in prior California rice research |
| Extended CDL crop history | 2007–2025 | Long-term public crop-history analysis |
| MODIS / CDL / NASS analysis | 2007–2025 | Long-term satellite and agricultural-outcome analysis |
| NASA SMAP core period | 2015–2025 | NASA soil-moisture fusion |
| Complete winter-to-rice SMAP comparison | 2016–2025 | Complete cross-year winter window |

---

## Quick start — run the Colab experiment

For the hackathon demonstration, **you do not need to rerun the full Steps 20–30 data-preparation pipeline**.

The analysis-ready data required by the Colab notebook have already been extracted and packaged as:

```text
data.zip
```

`data.zip` is stored in the repository root and contains:

```text
data.zip
├── step24/
├── step25/
├── step27/
├── step28/
├── step29/
└── step30/
```

### Run in Google Colab

1. Open `notebooks/RiceShift_Submission_Experiment.ipynb` in Google Colab.
2. Download `data.zip` from this repository.
3. In Colab, open the **Files** panel on the left.
4. Drag and drop `data.zip` directly into the Colab runtime.
5. Confirm that it appears as:

```text
/content/data.zip
```

6. Run the notebook from top to bottom.

**Google Drive is not required.**

The notebook automatically extracts `data.zip` and reads the analysis-ready Step folders.

---

## Data-preparation pipeline

The repository contains the Ubuntu 24.04 data-preparation workflow used to create the compact analysis-ready outputs in `data.zip`.

**Most users only need `data.zip` and the Colab notebook.**  
Steps 20–30 are provided for transparency, reproducibility, and for users who want to rebuild the analysis-ready dataset from source data.

```text
Step 20  Raw native data download + georeferencing
    ↓
Step 21  CDL rice masks + native-grid rice fractions
    ↓
Step 22  MODIS physical-value scaling + QA rules
    ↓
Step 23  Sacramento Valley rice-area spatial summaries
    ↓
Step 24  Monthly / seasonal / winter / annual features
    ↓
Step 25  USDA NASS outcomes and feature joins
    ↓
Step 26  NASA environmental downloader
    ↓
Step 27  SMAP analysis-ready feature extraction
    ↓
Step 28  CDL crop-rotation history
    ↓
Step 29  SSURGO soil preparation
    ↓
Step 30  Final data-coverage preflight
```

### Public pipeline numbering

During development:

- the NASA environmental downloader was temporarily called **Step 27**
- the SMAP feature extractor was temporarily called **Step 27.ex**

For the public repository, the numbering is normalized to:

- **Step 26 — NASA environmental downloader**
- **Step 27 — SMAP feature extraction**

The scientific workflow is unchanged.

---

## Repository structure

```text
RiceShift/
├── README.md
├── requirements.txt
├── config.py
├── config.example.py
├── .gitignore
├── data.zip
│
├── scripts/
│   ├── 20_california_rice_raw_native_downloader.py
│   ├── 21_cdl_rice_mask_and_fraction.py
│   ├── 22_riceshift_remote_sensing_physical_rules.py
│   ├── 23_riceshift_sacramento_spatial_summary.py
│   ├── 24_riceshift_analysis_ready_features.py
│   ├── 25_nass_outcomes_and_join.py
│   ├── 26_riceshift_nasa_environment_downloader.py
│   ├── 27_riceshift_smap_features.py
│   ├── 28_riceshift_cdl_rotation_history.py
│   ├── 29_riceshift_ssurgo_soil_features.py
│   └── 30_riceshift_data_preflight.py
│
└── notebooks/
    └── RiceShift_Submission_Experiment.ipynb
```

---

## Local processing environment

The data-preparation pipeline was developed and executed on **Ubuntu 24.04 LTS**.

It does not depend on a specific cloud provider or hosted service. The same workflow can be run on a suitable local or virtual Linux environment, including:

- a native Ubuntu 24.04 PC or workstation
- Windows 11 with **WSL2 + Ubuntu 24.04**
- a Linux virtual machine
- a cloud-hosted Ubuntu 24.04 instance

The Python environment used for the project is documented in `requirements.txt`.

Install with:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Some geospatial workflows additionally require GDAL command-line tools at the operating-system level.

Example for Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y gdal-bin
```

---

## Credentials

The public `config.py` and `config.example.py` contain **placeholders only**.

### NASA Earthdata Login

Create or manage an account:

https://urs.earthdata.nasa.gov/

Registration instructions:

https://urs.earthdata.nasa.gov/documentation/for_users/how_to_register

### USDA NASS Quick Stats API Key

Request a free API key:

https://quickstats.nass.usda.gov/api/

**Never commit real usernames, passwords, API keys, or tokens to a public repository.**

---

## Colab experiment

The Colab notebook demonstrates the RiceShift analysis in a compact sequence:

```text
Upload data.zip
        ↓
Automatic extraction
        ↓
Data integrity check
        ↓
19-year crop-history analysis
        ↓
NASA SMAP seasonal soil-moisture analysis
        ↓
NASA / NASS integrated analysis
        ↓
Exploratory yield relationships
        ↓
SSURGO soil context
        ↓
RiceShift strategy comparison
```

---

## Scientific interpretation

RiceShift uses observational satellite, crop-history, soil, and agricultural-statistics data.

Therefore:

- observed correlations are **not interpreted as causal effects**
- the prototype does **not claim that one rotation is universally optimal**
- recommendations are treated as **decision-support candidates**
- operational recommendations would require field-level agronomic and economic validation

---

## Project status

This repository documents the complete RiceShift data-preparation workflow and the hackathon analysis prototype developed for:

**NASA Space Apps Challenge 2026**  
**Challenge: Field Shift: Adapting Farms with NASA Data**

The project demonstrates how NASA Earth observations can be combined with long-term crop history, soil information, and agricultural outcomes to support more context-aware crop-rotation decisions for California rice farms.
