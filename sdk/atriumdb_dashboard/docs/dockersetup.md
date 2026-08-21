# Docker Setup Guide — AtriumDB SDK

The AtriumDB SDK uses a Linux-only C library for waveform storage. Running the SDK
or its tests directly on macOS is not supported. Docker provides a Linux environment
on your Mac so you can build, run, and iterate on the SDK normally.

---

## 1. Install Docker Desktop (macOS)

1. Go to https://www.docker.com/products/docker-desktop/ and click **Download for Mac**.
   Choose the chip that matches your Mac:
   - **Apple Silicon (M1/M2/M3/M4)** → download the Apple Silicon installer
   - **Intel Mac** → download the Intel installer

2. Open the downloaded `.dmg` file and drag **Docker** into your Applications folder.

3. Launch Docker from Applications. A whale icon appears in the menu bar.
   Wait until it stops animating and shows **"Docker Desktop is running"** — this
   takes about 30 seconds on first launch.

4. Verify Docker is working by opening a terminal and running:
   ```bash
   docker --version
   ```
   You should see something like `Docker version 27.x.x`.

---

## 2. Navigate to the SDK directory

All commands below must be run from the `sdk/` directory — the folder that contains
the `Dockerfile`:

```bash
cd /path/to/atriumdb/sdk
```

---

## 3. Build the Docker image

This step downloads the base Python image, installs system libraries, and installs
the SDK with all testing dependencies. It only needs to be re-run when `pyproject.toml`
or the `Dockerfile` itself changes.

```bash
docker build -t atriumdb-sdk .
```

- `-t atriumdb-sdk` gives the image a name (`atriumdb-sdk`) so you can refer to it
  by name instead of a hash.
- `.` tells Docker to use the `Dockerfile` in the current directory.

The first build takes a few minutes (downloading layers, compiling the MariaDB C
extension). Subsequent builds are much faster because Docker caches unchanged layers.

---

## 4. Mount a real dataset (optional)

The host path to your dataset is stored in `sdk/docker-run-dataset.sh` (gitignored).
Open that file and set `HOST_DATASET_PATH` to your absolute Mac path — that is the only
place you ever need to edit. Then run the script from the `sdk/` directory:

```bash
chmod +x docker-run-dataset.sh   # one-time, makes the script executable
./docker-run-dataset.sh
```

This drops you into an interactive shell inside the container with:
- your dataset mounted at `/data/atriumdb` (where the SDK expects `meta/index.db` and `tsc/`)
- the `sdk/` source tree mounted at `/sdk` so edits on your Mac are live inside the container
- `ATRIUMDB_DATASET_LOCATION=/data/atriumdb` injected from `.env`

To run a specific test file instead of opening a shell, pass the pytest command as an argument:

```bash
./docker-run-dataset.sh python -m pytest tests/test_dashboard_real_data.py -v -s
```
To run a specific function in the file.
```base
./docker-run-dataset.sh python -m pytest tests/test_dashboard_real_data.py::test_inspect_real_dataset -v -s
```

The `"$@"` in the script forwards any arguments you append before `atriumdb-sdk bash`.

---

## 5. Run the dashboard tests

```bash
docker run --rm atriumdb-sdk
```

- `--rm` automatically deletes the container when the test run finishes (keeps your
  system tidy; omit it if you want to inspect the container after a failure).
- The default command in the `Dockerfile` runs `tests/test_dashboard_api.py`.

Expected output (all assertions pass):

```
tests/test_dashboard_api.py::test_api_cohorts
Testing 1A: MRN cohort endpoint...
Testing 1B: demographic cohort — location filter...
Testing 1B: demographic cohort — sex filter...
Testing 1B: demographic cohort — age filter...
Testing 1B: demographic cohort — multiple cohorts in one request...
PASSED

====== 1 passed in X.XXs ======
```

---

## 6. Run a specific test or the full test suite

Override the default command by appending your own `pytest` invocation:

```bash
# Run only the dashboard test file
docker run --rm atriumdb-sdk python -m pytest tests/test_dashboard_api.py -v -s

# Run a single test function
docker run --rm atriumdb-sdk python -m pytest tests/test_dashboard_api.py::test_api_cohorts -v -s

# Run the entire test suite
docker run --rm atriumdb-sdk python -m pytest tests/ -v
```

---

## 7. Open an interactive shell inside the container

Useful for exploring, debugging, or running ad-hoc Python code against the SDK:

```bash
docker run --rm -it atriumdb-sdk bash
```

Once inside you can, for example:

```bash
# Run pytest manually
python -m pytest tests/test_dashboard_api.py -v -s

# Start a Python interpreter with the SDK importable
python3
>>> from atriumdb.atrium_sdk import AtriumSDK
>>> sdk = AtriumSDK.create_dataset(dataset_location="/tmp/test_db", database_type="sqlite")
```

Type `exit` to leave the container.

---

## 8. Iterate on the source code without rebuilding

Mount your local `sdk/` directory into the container so that edits on your Mac are
immediately visible inside the container — no rebuild needed:

```bash
docker run --rm -it \
  -v "$(pwd)":/sdk \
  atriumdb-sdk bash
```

- `-v "$(pwd)":/sdk` replaces the container's `/sdk` directory with your local
  working copy.
- Any file you edit on your Mac is instantly available inside the container.
- Run `python -m pytest tests/test_dashboard_api.py -v -s` inside the shell to
  re-run tests after each change.

> Note: the image still needs to have been built at least once with `docker build`
> so that system libraries and Python packages are installed. The `-v` flag only
> mounts source files, not the installed packages.

---

## 9. Rebuild after dependency changes

If you add or change a dependency in `pyproject.toml`, or change the `Dockerfile`
itself, rebuild the image:

```bash
docker build -t atriumdb-sdk .
```

---

## Quick-reference cheat sheet

| Task | Command |
|---|---|
| Build image | `docker build -t atriumdb-sdk .` |
| Run dashboard tests | `docker run --rm atriumdb-sdk` |
| Run all tests | `docker run --rm atriumdb-sdk python -m pytest tests/ -v` |
| Interactive shell | `docker run --rm -it atriumdb-sdk bash` |
| Shell with live source | `docker run --rm -it -v "$(pwd)":/sdk atriumdb-sdk bash` |
| Shell with real dataset | `./docker-run-dataset.sh` |
| Run real-data tests | `./docker-run-dataset.sh python -m pytest tests/test_dashboard_real_data.py -v -s` |
| Remove image | `docker rmi atriumdb-sdk` |