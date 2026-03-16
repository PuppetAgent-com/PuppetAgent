# Self-Hosting PuppetAgent

Step-by-step guide to running PuppetAgent Studio on your own GPU machine.

---

## What you need

| Component | Minimum |
|-----------|---------|
| OS | Ubuntu 22.04+ or Debian 12+ (headless is fine) |
| GPU | NVIDIA RTX 2080+ or AMD RX 6700+ with ROCm |
| RAM | 16 GB (32 GB recommended for large models) |
| Disk | 100 GB free (GPU tool installs are large) |
| Python | 3.10+ |
| conda | miniconda or anaconda |
| Docker | For MinIO + Traefik |

---

## Step 1 — Install GPU tools

These are the heavy prerequisites. Each runs in its own conda env.

### FaceFusion (face swap)
```bash
git clone https://github.com/facefusion/facefusion.git ~/facefusion
cd ~/facefusion
conda create -n facefusion python=3.10 -y
conda activate facefusion
pip install -r requirements.txt
```

### MuseTalk (lip sync)
```bash
git clone https://github.com/TMElyralab/MuseTalk.git ~/MuseTalk
cd ~/MuseTalk
conda create -n musetalk python=3.10 -y
conda activate musetalk
pip install -r requirements.txt
```

### XTTS (voice cloning)
```bash
python3 -m venv ~/venvs/xtts
~/venvs/xtts/bin/pip install TTS deepfilternet
```

---

## Step 2 — Install MinIO (Docker)

```bash
docker run -d --name minio-video \
  --restart unless-stopped \
  -p 9004:9000 -p 9005:9001 \
  -e MINIO_ROOT_USER=minio_admin \
  -e MINIO_ROOT_PASSWORD=yourpassword \
  -v ~/minio-video:/data \
  minio/minio server /data --console-address ":9001"
```

---

## Step 3 — Install PuppetAgent Studio

```bash
git clone https://github.com/PuppetAgent/PuppetAgent.git ~/PuppetAgent-src
python3 -m venv ~/venvs/puppetagent
~/venvs/puppetagent/bin/pip install flask minio

# Deploy studio files
mkdir -p ~/PuppetAgent/data
cp ~/PuppetAgent-src/studio/app.py ~/PuppetAgent/
cp -r ~/PuppetAgent-src/studio/static ~/PuppetAgent/

# Deploy processor scripts
cp ~/PuppetAgent-src/processor/video-processor.py ~/scripts/
cp ~/PuppetAgent-src/processor/auto-edit.py ~/scripts/
cp ~/PuppetAgent-src/processor/voice-enhance.py ~/scripts/
cp ~/PuppetAgent-src/processor/bg-replacement.py ~/scripts/
```

---

## Step 4 — Configure

```bash
mkdir -p ~/.puppetagent
cp ~/PuppetAgent-src/puppetagent.conf.example ~/.puppetagent/puppetagent.conf
nano ~/.puppetagent/puppetagent.conf
```

Edit all paths to match your system. Then verify:
```bash
python3 ~/scripts/video-processor.py --check-config
```

---

## Step 5 — Add influencer assets

For each influencer (e.g. `emma`), create:
```
~/influencers/emma/
├── portrait.jpg          face source for FaceFusion
├── voice-sample.wav      voice sample for XTTS cloning (10-30s clear speech)
├── neutral-loop.mp4      neutral talking-head video for MuseTalk
├── background.png        background image for bg-replacement
└── ambient.wav           ambient audio for audio mix
```

---

## Step 6 — Start the service

```bash
cp ~/PuppetAgent-src/infra/puppetagent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now puppetagent

# Verify
curl http://localhost:9005/api/jobs
```

---

## Step 7 — Expose publicly (Traefik + Cloudflare)

1. Copy Traefik config:
   ```bash
   cp ~/PuppetAgent-src/infra/traefik/puppetagent.yml ~/docker/traefik/dynamic/
   ```

2. Edit `puppetagent.yml` — update the htpasswd hash:
   ```bash
   htpasswd -nb youruser yourpassword
   # paste result into the basicAuth users list
   ```

3. Configure Cloudflare Tunnel:
   - Zero Trust → Networks → Tunnels → your tunnel → Public Hostname
   - Domain: `puppetagent.com` → Service: `http://traefik:80`

4. Verify:
   ```bash
   curl -u youruser:yourpassword https://puppetagent.com/api/jobs
   ```

---

## Service management

```bash
systemctl --user status puppetagent
systemctl --user restart puppetagent
journalctl --user -u puppetagent -f

# Internal health check (no auth):
curl http://localhost:9005/api/jobs

# Check Traefik picked up config:
docker logs traefik --tail 20 | grep -E "file|puppetagent|ERR"
```

---

## Troubleshooting

**Studio not starting**
```bash
journalctl --user -u puppetagent -n 50
# Check: is venv path correct? Is port 9005 free?
```

**MinIO connection refused**
```bash
curl http://localhost:9004/minio/health/live
# Check: is minio-video container running?
docker ps | grep minio-video
```

**video-processor.py fails at face_swap**
```bash
# Check FaceFusion conda env
conda activate facefusion
python -c "import facefusion"
# Verify GPU is accessible
nvidia-smi   # NVIDIA
rocm-smi     # AMD
```

**Videos not playing in dashboard**
```bash
# Check MINIO_PUBLIC matches your domain
grep MINIO_PUBLIC ~/.puppetagent/puppetagent.conf
# Check /videos route is working
curl https://puppetagent.com/videos/
```
