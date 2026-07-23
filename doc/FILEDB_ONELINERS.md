# (WSL) Pipe all charters' atom_ids into into clipboard
```bash
awk '{print $0}' */*/*/atom_id.txt | clip.exe
```

# Get A list of all charters with no (downloaded) images

```bash
ls $(echo */*/*/image_urls.json) -l|awk '{ print $9 " "  $5 }'|grep -e ' 2$'
```


```bash
NOIMAGE_DIRS=$(ls $(echo */*/*/image_urls.json) -l|awk '{ print $9 " "  $5 }'|grep -e ' 2$'|xargs dirname|grep -e '...*')
for DIRNAME in $NOIMAGE_DIRS;
do
echo "$DIRNAME/failed.txt"
done;
#scroll up to see the f*.txt vs failed.txt
```


Install DiDipCV
```bash
WORK_SRC_DIR="/tmp"
(cd "${WORK_SRC_DIR}"; git clone --recursive git@github.com:anguelos/didipcv.git )
(cd "${WORK_SRC_DIR}/didipcv"; git submodule update --init --recursive)
(cd "${WORK_SRC_DIR}/didipcv"; git submodule foreach --recursive git reset --hard) # Chat GPT told me this might be needed
(cd "${WORK_SRC_DIR}/didipcv"; git submodule update --init --recursive)
```
The last line should be there instead of pull.
To be tested more throughly but a git pull globally should also followed by a git submodule update




Create FSDB archive
```bash
tar -cpvzf ~/tmp/fsdb_1000cv_v0_0_1.tar.gz */*/*/*.img.* */*/*/url.txt */*/*/atom_id.txt */*/*/image_urls.json */*/*/cei.xml */*/*/index.html */*/*/original.html
```
Duration (time) 1m30,423s

Deploy FSDB archive
```bash
#https://cloud.uni-graz.at/s/e4J8LzaZ8ax9QMy
tar -xpvzf fsdb_1000cv_v0_0_1.tar.gz
```
Duration (time) 0m17,578s

Deploy FSDB archive from unicloud
```bash
mkdir -p ~/tmp/1000CV
cd ~/tmp/1000CV
curl https://cloud.uni-graz.at/s/e4J8LzaZ8ax9QMy/download/fsdb_1000cv_v0_0_1.tar.gz | tar zxf -
```
Duration (time) 2m0,001s

Launch FSDB
```bash
/home/anguelos/work/src/didipcv
PYTHONPATH="/home/anguelos/work/src/didipcv/src/" ./bin/ddp_serve_fsdb 
```



Serve CEI2JSON
```bash
DIDIP_ROOT=/home/anguelos/work/src/didipcv
PYTHONPATH="${DIDIP_ROOT}/src/" "${DIDIP_ROOT}/apps/ddpa_cei2json/bin/ddp_cei2json_serve"
```


Compute CEI2JSON
```bash
DIDIP_ROOT=/home/anguelos/work/src/didipcv
FSDB_ROOT="./"
PYTHONPATH="/home/anguelos/work/src/didipcv/src/" "${DIDIP_ROOT}/apps/ddpa_cei2json/bin/ddp_cei2json_compute" -charter_paths "${FSDB_ROOT}"/*/*/*/
```

Compute Seals
```bash
curl https://cloud.uni-graz.at/s/mYbgNdDRqFw8Qkf/download/ddp_yolov5.pt > /tmp/ddp_yolov5.pt
CREATE_DIRS=1
SHOW_INTERACTIVE=0
DIDIP_ROOT=/home/anguelos/work/src/didipcv
FSDB_ROOT="./"
PYTHONPATH="/home/anguelos/work/src/didipcv/src/:${DIDIP_ROOT}/apps/ddpa_seals" "${DIDIP_ROOT}/apps/ddpa_seals/bin/ddp_seals_detect" -img_paths "${FSDB_ROOT}"/*/*/*/*.img.* -weights /tmp/ddp_yolov5.pt -save_crops "${CREATE_DIRS}" -preview "${SHOW_INTERACTIVE}"
```
