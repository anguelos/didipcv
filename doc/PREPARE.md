
### Mounting Images 

```bash
REMOTE='grazgpu' # atzenhofer@143.50.30.63
mkdir -p ./data/icarus_mirror/
sshfs grazgpu:/data/anguelos/monasterium/ ./data/icarus_mirror/  -o ro
```
After mounting, you should only care about

### Mounting the database
If the XML database dump is not in didip/data/db, you can put it in with
```bash
XML_DB_ROOT='/home/anguelos/work/monasterium/db/'
DIDIP_GIT_ROOT='/home/anguelos/work/monasterium/didip/'
ln -s "$XML_DB_ROOT" "${DIDIP_GIT_ROOT}data/db"
```

### Python requirements
```bash
pip install --user tqdm lxml fargv pandas
```

### UNIX requirements
```bash
sudo apt-get install sshfs
```