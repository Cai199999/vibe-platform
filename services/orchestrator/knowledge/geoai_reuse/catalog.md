# GeoAI 复用目录（本地）

> 生成时间：2026-11-21T07:27:55.324764+00:00
> 只读参考：不是运行时依赖，也不会自动安装；许可证与维护状态需在复用前复核。

## 怎么用

- 需求涉及遥感/测绘/GIS + AI 时，先看本目录有没有可复用方案；
- 输出建议时标注 **REUSE / ADAPT / BUILD**；
- 当前平台 CodeGen 白名单只放行 React/Vite/Tailwind 与本地模块；
  Cesium/OpenLayers/GDAL/PostGIS 等属于“外部组件/后端能力”，先出集成方案，不自动生成。

## 候选项目

### opengeos/geoai

- URL：https://github.com/opengeos/geoai
- License：MIT · Stars：3479 · 最近更新：2026-11-21 · 语言：Python
- 定位：Python GeoAI 库：地理空间数据 + AI 模型/流程
- 可复用：后端地理处理、遥感推理、数据准备；不适合直接生成前端
- 描述：GeoAI: Artificial Intelligence for Geospatial Data
- README 摘要：# GeoAI: Artificial Intelligence for Geospatial Data [ ](https://pypi.python.org/pypi/geoai-py) [ ](https://pepy.tech/project/geoai-py) [ ](https://anaconda.org/conda-forge/geoai) [ ](https://anaconda.org/conda-forge/geoai) [ ](https://github.com/conda-forge/geoai-py-feedstock) [ ](https://opensource.org/licenses/MIT) [ ](https://www.youtube.com/playlist?list=PLAxJ4-o7ZoPcvENqwaPa_QwbbkZ5sctZE) [ ](https://opengeoai.org/qgis_plugin) [ ](https://doi.org/10.21105/joss.09605) [ ](https://github.com/opengeos/geoai/blob/master/docs/assets/logo.png) **A powerful Python package for integrating artificial intelligence with geospatial data analysis and visualization** ## 📖 Introduction [GeoAI](https://opengeoai.org) is a comprehensive Python package designed to bridge artificial intelligence (AI) and geospatial data analysis, providing researchers and practitioners with intuitive tools for applyi

### decision-labs/geoai.js

- URL：https://github.com/decision-labs/geoai.js
- License：MIT · Stars：200 · 最近更新：2026-09-29 · 语言：TypeScript
- 定位：浏览器端 GeoAI：transformers.js + 前端推理
- 可复用：前端地图/影像推理模式参考；当前平台 CodeGen 未接该依赖
- 描述：🛰️ GeoAI.js is a javascript library for use with transformers.js to perform GeoAI on the frontend
- README 摘要：GeoAI.js Geospatial AI that runs in the browser. Detect buildings, vehicles, ships, solar panels, and more — on satellite &amp; aerial imagery — without a GPU backend. Live demos → · Docs → · Agent skill → DINOv3 image feature extraction in the browser — try it at docs.geobase.app/geoai-live --- ## Why GeoAI.js? | | | |---|---| | **Client-side** | Models run in the browser (WebGPU / WASM) via Transformers.js + ONNX Runtime | | **Draw → detect** | Pass a GeoJSON polygon; get detections back as GeoJSON | | **Your imagery** | ESRI, Mapbox, Geobase COGs, TMS, WMS, OpenAerialMap, Google Map Tiles | | **Pipeline-ready** | Chain tasks; run inference off the main thread with workers | ```bash npm i geoai # peers npm i @huggingface/transformers onnxruntime-web ``` ```javascript import { geoai } from "geoai"; const pipeline = await geoai.pipeline([{ task: "building-detection" }], { provider: "esri

### azavea/raster-vision

- URL：https://github.com/azavea/raster-vision
- License：Apache-2.0 · Stars：2246 · 最近更新：2026-06-04 · 语言：Python
- 定位：遥感/航拍影像深度学习框架
- 可复用：训练/推理流水线、切片、标注与评估参考
- 描述：An open source library and framework for deep learning on satellite and aerial imagery.
- README 摘要：&nbsp; [ ](https://pypi.org/project/rastervision/) [ ](https://docs.rastervision.io/en/stable/?badge=stable) [ ](https://opensource.org/licenses/Apache-2.0) [ ](https://github.com/azavea/raster-vision/actions/workflows/release.yml) [ ](https://codecov.io/gh/azavea/raster-vision) Raster Vision is an open source Python **library** and **framework** for building computer vision models on satellite, aerial, and other large imagery sets (including oblique drone imagery). It has built-in support for chip classification, object detection, and semantic segmentation with backends using PyTorch. **As a library**, Raster Vision provides a full suite of utilities for dealing with all aspects of a geospatial deep learning workflow: reading geo-referenced data, training models, making predictions, and writing out predictions in geo-referenced formats. **As a low-code framework**, Raster Vision allows 

### sentinel-hub/eo-learn

- URL：https://github.com/sentinel-hub/eo-learn
- License：MIT · Stars：1257 · 最近更新：2026-09-09 · 语言：Python
- 定位：Earth Observation 机器学习处理框架
- 可复用：卫星影像获取、预处理、EO 工作流参考
- 描述：Earth observation processing framework for machine learning in Python
- README 摘要：[ ](https://pypi.org/project/eo-learn) [ ](https://anaconda.org/conda-forge/eo-learn) [ ](https://pypi.org/project/eo-learn) [ ](https://github.com/sentinel-hub/eo-learn/actions) [ ](https://eo-learn.readthedocs.io) [ ](https://github.com/sentinel-hub/eo-learn/blob/master/LICENSE) [ ](https://pepy.tech/project/eo-learn) [ ](https://pepy.tech/project/eo-learn) [ ](https://hub.docker.com/r/sentinelhub/eolearn) [ ](https://codecov.io/gh/sentinel-hub/eo-learn) [ ](https://zenodo.org/badge/latestdoi/135559956) # eo-learn **eo-learn makes extraction of valuable information from satellite imagery easy.** The availability of open Earth observation (EO) data through the Copernicus and Landsat programs represents an unprecedented resource for many EO applications, ranging from ocean and land use and land cover monitoring, disaster control, emergency services and humanitarian relief. Given the larg

### MEKXH/golem

- URL：https://github.com/MEKXH/golem
- License：MIT · Stars：193 · 最近更新：2026-07-07 · 语言：Go
- 定位：自然语言驱动的 GeoAI Agent（GDAL/PostGIS 工具调用）
- 可复用：GIS Agent 的工具编排/意图拆解参考，不是运行时依赖
- 描述：Golem — The self-evolving GeoAI Agent for the geospatial industry — natural-language GDAL/PostGIS workflows, learned pipeline reuse, fabricated tool  scaffolding, approval & audit governance, accessible via WebUI, TUI, and IM channels. 
- README 摘要：# Golem (גּוֹלֶם) [ ](https://go.dev/) [ ](https://github.com/MEKXH/golem/releases/latest) [ ](https://shelldex.com/projects/golem/) [ ](LICENSE) **The self-evolving GeoAI Agent — your private GIS analyst.** Golem is a **vertical AI Agent for the geospatial industry**, built with [Go](https://go.dev/) and [Eino](https://github.com/cloudwego/eino). It bridges the gap between natural-language interaction and professional GIS workflows — turning complex GDAL/PostGIS operations into conversational requests accessible from a **WebUI**, **terminal TUI**, or **any IM channel**. Unlike generic chatbot wrappers, Golem ships with a real agent loop (tool calling up to 20 iterations), workspace-local GDAL/PostGIS tooling, learned pipeline reuse, fabricated tool scaffolding, and skill telemetry loops — all governed by a built-in approval and audit framework. > **Golem (גולם)**: In Jewish folklore, a 

### opengeos/qgis-samgeo-plugin

- URL：https://github.com/opengeos/qgis-samgeo-plugin
- License：MIT · Stars：130 · 最近更新：2026-11-17 · 语言：Python
- 定位：QGIS + SAM 分割插件
- 可复用：桌面 GIS 交互与影像分割工作流参考
- 描述：QGIS plugin for the SamGeo Python package
- README 摘要：# SamGeo QGIS Plugin [ ](https://doi.org/10.21105/joss.05663) [ ](https://github.com/opengeos/qgis-samgeo-plugin) [ ](https://github.com/opengeos/segment-geospatial) A QGIS plugin for remote sensing image segmentation using [SamGeo](https://samgeo.gishub.org), powered by Meta's Segment Anything Model ([SAM](https://github.com/facebookresearch/sam3)). ## Features - **Multiple SAM versions**: Support for SamGeo (SAM1), SamGeo2 (SAM2), and SamGeo3 (SAM3) - **Text-based segmentation**: Describe what you want to segment (e.g., "building", "tree", "road") - **Interactive point prompts**: Click on the map to add foreground/background points - **Box prompts**: Draw rectangles to segment specific regions - **GeoTIFF support**: Works with georeferenced images, preserving spatial reference - **Multiple output formats**: Save as vector (GeoPackage, Shapefile) or raster (GeoTIFF) ## Demos Check out t

### microsoft/torchgeo

- URL：https://github.com/torchgeo/torchgeo
- License：MIT · Stars：4243 · 最近更新：2026-11-20 · 语言：Python
- 定位：PyTorch 地理空间数据集/模型库
- 可复用：遥感数据集、预训练模型、训练管线参考
- 描述：TorchGeo: datasets, samplers, transforms, and pre-trained models for geospatial data
- README 摘要：TorchGeo is a [PyTorch](https://pytorch.org/) domain library, similar to [torchvision](https://docs.pytorch.org/vision), providing datasets, samplers, transforms, and pre-trained models specific to geospatial data. The goal of this library is to make it simple: 1. for machine learning experts to work with geospatial data, and 2. for remote sensing experts to explore machine learning solutions. Community: [ ](https://torchgeo.slack.com/join/shared_invite/zt-22rse667m-eqtCeNW0yI000Tl4B~2PIw) [ ](https://www.osgeo.org/community/getting-started-osgeo/) [ ](https://github.com/sponsors/torchgeo) [ ](https://huggingface.co/torchgeo) [ ](https://zenodo.org/communities/torchgeo/records) [ ](https://pytorch.org/join-ecosystem/) [ ](https://www.youtube.com/@TorchGeo) Packaging: [ ](https://pypi.org/project/torchgeo/) [ ](https://anaconda.org/conda-forge/torchgeo) [ ](https://packages.spack.io/packa

### deepVector/geospatial-machine-learning

- URL：https://github.com/deepVector/geospatial-machine-learning
- License：MIT · Stars：709 · 最近更新：2018-06-21 · 语言：
- 定位：地理空间机器学习资源清单
- 可复用：选型发现入口，不是代码依赖
- 描述：A curated list of resources focused on Machine Learning in Geospatial Data Science.
- README 摘要：# geospatial-machine-learning A curated list of resources focused on Machine Learning in Geospatial Data Science. ## Table of Contents * [Code projects and Workflows](#code-projects-and-workflows) * [Datasets](#datasets) * [Papers](#papers) * [Books](#books) * [Courses](#courses) * [Companies](#companies) ## Code projects and Workflows * [A 2017 Guide to Semantic Segmentation with Deep Learning](http://blog.qure.ai/notes/semantic-segmentation-deep-learning-review) (2017) by Sasank Chilamkurthy | qure.ai * [Deeplab Image Semantic Segmentation Network](https://sthalles.github.io/deep_segmentation_network/) (2018) by Thalles Silva | sthalles.github.io * [deeplab_v3](https://github.com/anxiangSir/deeplab_v3) by anxiangSir | Github * [deeplab_v3: Tensorflow Implementation of the Semantic Segmentation DeepLab_V3 CNN](https://github.com/sthalles/deeplab_v3) by Thalles Silva | Github * [Deep lea

## 选型提示

- **前端地图/浏览器推理**：参考 `decision-labs/geoai.js`；是否引入取决于前端依赖白名单。
- **遥感训练/推理**：`microsoft/torchgeo`、`azavea/raster-vision`、`sentinel-hub/eo-learn`。
- **GIS Agent/工具编排**：`MEKXH/golem` 的意图拆解与 GDAL/PostGIS 工具调用值得参考。
- **QGIS 桌面工作流**：`opengeos/qgis-samgeo-plugin`。
- **资源发现**：`deepVector/geospatial-machine-learning`。

> 复用建议必须附带：许可证、维护状态、集成成本、与当前契约的边界；不得自动安装或执行仓库代码。
