<!-- PROJECT INTRO -->

OrpheusDL - Qobuz
=================

A Qobuz module for the OrpheusDL modular archival music program

[Report Bug](https://github.com/yarrm80s/orpheusdl/issues)
·
[Request Feature](https://github.com/yarrm80s/orpheusdl/issues)


## Table of content

- [About OrpheusDL - Qobuz](#about-orpheusdl-qobuz)
- [Getting Started](#getting-started)
    - [Prerequisites](#prerequisites)
    - [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
    - [Global](#global)
    - [Qobuz](#qobuz)
- [Contact](#contact)


<!-- ABOUT ORPHEUS -->
## About OrpheusDL - Qobuz

OrpheusDL - Qobuz is a module written in Python which allows archiving from **Qobuz** for the modular music archival program.


<!-- GETTING STARTED -->
## Getting Started

Follow these steps to get a local copy of Orpheus up and running:

### Prerequisites

* Already have [OrpheusDL](https://github.com/yarrm80s/orpheusdl) installed

### Installation

1. Clone the repo inside the folder `orpheusdl/modules/`
   ```sh
   git clone https://github.com/yarrm80s/orpheusdl-qobuz.git qobuz
   ```
2. Execute:
   ```sh
   python orpheus.py
   ```
3. Now the `config/settings.json` file should be updated with the Qobuz settings

<!-- USAGE EXAMPLES -->
## Usage

Just call `orpheus.py` with any link you want to archive:

```sh
python orpheus.py https://open.qobuz.com/album/c9wsrrjh49ftb
```

<!-- CONFIGURATION -->
## Configuration

You can customize every module from Orpheus individually and also set general/global settings which are active in every
loaded module. You'll find the configuration file here: `config/settings.json`

### Global

```json5
"global": {
    "general": {
        // ...
        "download_quality": "hifi"
    },
    "formatting": {
        "album_format": "{album_name}{quality}{explicit}",
        // ...
    }
    // ...
}
```

`download_quality`: Choose one of the following settings:
* "hifi": FLAC up to 192/24
* "lossless": FLAC with 44.1/16
* "high": MP3 320 kbit/s

`album_format`:
* `{quality}` will add the format which you can specify under `quality_format` (see below), default:
    ```
     [192kHz 24bit]
    ```
  depending on the maximum available album quality and the chosen `download_quality` setting
* `{explicit}` will add
    ```
     [E]
    ```
  to the album path 

### Qobuz
```json5
"qobuz": {
    "app_id": "",
    "app_secret": "",
    "quality_format": "{sample_rate}kHz {bit_depth}bit",
    "username": "",
    "password": ""
}
```
`app_id` / `app_secret`: Qobuz tokens are bound to the app_id they were created with, so the pair must match your token's creation date:

* Created after 2025-05-06: `app_id` `798273057`, `app_secret` `abb21364945c0583309667d13ca3d93a`
* Created after 2024-08-12: `app_id` `579939560`, `app_secret` `fa31fc13e7a28e7d70bb61e91aa9e178`
* Created before 2024-08-12: `app_id` `950096963`, `app_secret` `979549437fcc4a3faad4867b5cd25dcb`

`quality_format`: How the quality is formatted when `{quality}` is present in `album_format`, possible values are 
`{sample_rate}` and `bit_depth`.

**NOTE: Set the `"quality_format": ""` to remove the quality string even if `{quality}` is present in `album_format`. 
Square brackets `[]` will always be added before and after the `quality_format` in the album path.**

`username`: Enter your qobuz email address here

`password`: Enter your qobuz password here

<!-- Contact -->
## Contact

Yarrm80s - [@yarrm80s](https://github.com/yarrm80s)

Dniel97 - [@Dniel97](https://github.com/Dniel97)

Project Link: [OrpheusDL Qobuz Public GitHub Repository](https://github.com/yarrm80s/orpheusdl-qobuz)
