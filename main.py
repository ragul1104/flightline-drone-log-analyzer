from __future__ import annotations

import math
import os
import tempfile
import threading
import time
import uuid
import heapq
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Flight Log Analyzer", docs_url=None, redoc_url=None)
ROOT = Path(__file__).parent
MAX_UPLOAD_MB = max(1, int(os.getenv("FLIGHTLINE_MAX_UPLOAD_MB", "1024")))
MAX_UPLOAD = MAX_UPLOAD_MB * 1024 * 1024
MAX_REPORT_ROWS = 5000
JOB_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="flight-log")
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def finite(value: Any):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
        return value if math.isfinite(value) else None
    return value


def norm_record(t, **fields):
    return {"t": finite(t), **{k: finite(v) for k, v in fields.items() if v is not None}}


class RowCollector:
    """Keep exact full-log counts while retaining a bounded, evenly spaced plot sample."""
    def __init__(self):
        self.rows=[]; self.count=0; self.stride=1; self.first_time=None; self.last_time=None
        self.last_row=None; self.field_counts=Counter(); self.fields=set()

    def add(self, row):
        idx=self.count; self.count+=1
        t=row.get("t")
        if self.first_time is None: self.first_time=t
        if t is not None: self.last_time=t
        self.last_row=row
        for key,value in row.items():
            if key!="t":
                self.fields.add(key)
                if value is not None: self.field_counts[key]+=1
        if idx % self.stride==0: self.rows.append(row)
        if len(self.rows)>=MAX_REPORT_ROWS*2:
            self.rows=self.rows[::2]
            self.stride*=2

    def result(self):
        rows=self.rows[:]
        if self.last_row is not None and (not rows or rows[-1] is not self.last_row):
            if len(rows)>=MAX_REPORT_ROWS: rows[-1]=self.last_row
            else: rows.append(self.last_row)
        quality={key:round(100*self.field_counts.get(key,0)/self.count) for key in self.fields} if self.count else {}
        return rows, {"decoded_sample_count":self.count,"display_sample_stride":self.stride,
                      "decoded_duration_s":(self.last_time-self.first_time) if self.first_time is not None and self.last_time is not None else 0,
                      "decoded_fields":sorted(self.fields),"decoded_quality":quality}


def parse_blackbox(path: str):
    from orangebox import Parser
    from orangebox.reader import Reader
    class StreamingHeaderReader(Reader):
        def _find_pointers(self, f):
            start=f.tell(); pattern=f.readline(); f.seek(start)
            if not pattern: return
            overlap=max(0,len(pattern)-1); carry=b""; offset=start; last=-1
            while True:
                chunk=f.read(4*1024*1024)
                if not chunk: break
                blob=carry+chunk; base=offset-len(carry); pos=0
                while True:
                    found=blob.find(pattern,pos)
                    if found<0: break
                    absolute=base+found
                    if absolute>last:
                        self._log_pointers.append(absolute); last=absolute
                    pos=found+max(1,len(pattern))
                carry=blob[-overlap:] if overlap else b""
                offset+=len(chunk)
    p = Parser(StreamingHeaderReader(path, 1))
    names = p.field_names
    headers = p.headers
    collector=RowCollector()
    for frame in p.frames():
        row = dict(zip(names, frame.data))
        t = row.get("time")
        if t is None: continue
        # Blackbox timestamps are microseconds. Native units are retained in raw.
        fields = {k: v for k, v in row.items() if k != "time" and isinstance(v, (int, float))}
        collector.add(norm_record(float(t) / 1_000_000, **fields))
    fw = f'{headers.get("Firmware type", "")} {headers.get("Firmware revision", "")}'.lower()
    firmware = "iNav Blackbox" if "inav" in fw else "Betaflight Blackbox" if "betaflight" in fw else "Blackbox (firmware not identified)"
    rows,stats=collector.result()
    return firmware, rows, names, {"headers": headers, "home": p.home_coords, "parser": "Orangebox", **stats}


def parse_ulog(path: str):
    from pyulog import ULog
    u = ULog(path)
    datasets=[]; heap=[]; fields=set()
    for d in u.data_list:
        data=d.data; stamps=data.get("timestamp")
        if stamps is None or len(stamps)==0: continue
        dataset=(d.name,data,stamps); datasets.append(dataset)
        fields.update(f"{d.name}.{key}" for key in data if key!="timestamp")
        heapq.heappush(heap,(int(stamps[0]),len(datasets)-1,0))
    collector=RowCollector()
    first_stamp=heap[0][0] if heap else 0
    while heap:
        stamp=heap[0][0]; row={"t":(stamp-first_stamp)/1e6}
        while heap and heap[0][0]==stamp:
            _,di,index=heapq.heappop(heap); topic,data,stamps=datasets[di]
            for key,values in data.items():
                if key!="timestamp":
                    value=values[index]
                    if hasattr(value,"tolist"): value=value.tolist()
                    row[f"{topic}.{key}"]=value
            next_index=index+1
            if next_index<len(stamps): heapq.heappush(heap,(int(stamps[next_index]),di,next_index))
        collector.add(row)
    rows,stats=collector.result(); names=sorted(fields)
    return "PX4 ULog", rows, names, {"parser": "pyulog", "initial_parameters": len(u.initial_parameters), "dropouts": len(u.dropouts), **stats}


def parse_dataflash(path: str):
    from pymavlink.DFReader import DFReader_binary
    reader = DFReader_binary(path)
    collector=RowCollector()
    while True:
        m = reader.recv_msg()
        if m is None: break
        typ = m.get_type()
        if typ in ("FMT", "FMTU", "PARM", "MSG", "VER", "UNIT", "MULT"): continue
        d = m.to_dict(); d.pop("mavpackettype", None)
        stamp_key = next((k for k in ("TimeUS", "TimeMS", "time_us", "time_ms") if isinstance(d.get(k), (int,float))), None)
        stamp = d.get(stamp_key) if stamp_key else None
        if stamp is None: continue
        t = float(stamp) / (1e6 if stamp_key.lower().endswith("us") else 1000)
        # Use first timestamp as zero; source-message fields stay explicit.
        if collector.count==0: t0 = t
        row = {"t": t-t0}
        for k,v in d.items():
            if isinstance(v,(int,float)): row[f"{typ}.{k}"] = finite(v)
        collector.add(row)
    if not collector.count: raise ValueError("No timestamped DataFlash records were found.")
    rows,stats=collector.result()
    return "ArduPilot DataFlash BIN", rows, sorted(collector.fields), {"parser":"pymavlink DFReader", **stats}


def compact(rows, cap=5000):
    if len(rows) <= cap: return rows
    last=len(rows)-1
    return [rows[round(i*last/(cap-1))] for i in range(cap)]


def analyze(firmware, rows, names, meta):
    if not rows: raise ValueError("The file was recognized, but no usable timestamped records were decoded.")
    # Normalize the bounded parser sample in place to avoid a second large copy.
    clean=rows
    for r in clean:
        for k,v in list(r.items()):
            if hasattr(v,"tolist"): v=v.tolist()
            if isinstance(v,(int,float)): r[k]=finite(v)
            elif isinstance(v,(list,str,bool)) or v is None: r[k]=v
            else: r.pop(k,None)
    times=[r["t"] for r in clean if isinstance(r.get("t"),(int,float))]
    start_time=min(times) if times else 0
    for r in clean:
        if isinstance(r.get("t"),(int,float)): r["t"]=round(r["t"]-start_time,4)
    duration=meta.get("decoded_duration_s",max(r["t"] for r in clean if isinstance(r.get("t"),(int,float))))
    # Native channel mapping is explicit by record/field names. No implicit conversion to RPM.
    allfields=sorted(set(meta.get("decoded_fields",[])) | {k for r in clean for k in r if k!="t"})
    if "decoded_quality" in meta:
        quality=meta["decoded_quality"]
    else:
        present_counts={k:0 for k in allfields}
        for r in clean:
            for k,v in r.items():
                if k!="t" and v is not None: present_counts[k]+=1
        quality={k: round(100*present_counts[k]/len(clean)) for k in allfields}
    missing=[k for k,pct in quality.items() if pct<95]
    events=[]; diagnoses=[]
    lowers=[k.lower() for k in allfields]
    if any("failsafe" in k or "ekf" in k or "crash" in k for k in lowers):
        candidates=[k for k in allfields if any(w in k.lower() for w in ("failsafe","ekf","crash"))]
        events.append({"t":None,"title":"Failure-related fields are present","detail":"Inspect the source fields in the graphs; field presence alone does not prove a failsafe or crash."})
        diagnoses.append({"title":"Failure status requires review","confidence":"low","status":"evidence present","evidence":candidates[:8],"reason":"Firmware-specific status fields exist; their semantics depend on firmware/version and are not interpreted as a confirmed cause."})
    for field in (k for k in allfields if "mode" in k.lower() or "armed" in k.lower()):
        missing_value=object()
        prior=missing_value
        for r in clean:
            value=r.get(field)
            if value is None: continue
            if prior is not missing_value and value != prior:
                events.append({"t":r.get("t"),"title":"Mode/arming field changed","detail":f"{field}: {prior} → {value} (raw recorded values; state labels not decoded)."})
            prior=value
    control_fields=[k for k in allfields if any(w in k.lower() for w in ("setpoint","desired","target_rate","rate_target"))]
    if control_fields:
        diagnoses.append({"title":"Control reference channels recorded","confidence":"medium","status":"recorded; tracking not scored","evidence":control_fields[:8],"reason":"Command/reference fields are present. A tracking error requires a firmware-specific matched feedback channel and unit conversion, which are not assumed here."})
    # Normalize position only for known field conventions. Keep original channels unchanged.
    def match_field(patterns):
        return next((k for k in allfields if any(p in k.lower() for p in patterns)), None)
    lat_key = match_field(["latitude", ".lat", "gps_coord[0]"])
    lon_key = match_field(["longitude", ".lon", ".lng", "gps_coord[1]"])
    position=[]
    alt_key = match_field(["gps_alt", "altitude", ".alt", "global_position.alt"])
    if lat_key and lon_key:
        is_px4_gps = "vehicle_gps_position" in lat_key.lower()
        is_px4_global = "vehicle_global_position" in lat_key.lower()
        scaled_e7 = is_px4_gps or firmware.startswith("ArduPilot") or "gps_coord" in lat_key.lower()
        for r in clean:
            lat,lon=r.get(lat_key),r.get(lon_key)
            if not isinstance(lat,(int,float)) or not isinstance(lon,(int,float)): continue
            if scaled_e7: lat,lon=lat/1e7,lon/1e7
            elif is_px4_global: lat,lon=math.degrees(lat),math.degrees(lon)
            point={"t":r.get("t"),"lat":lat,"lon":lon}
            if alt_key and isinstance(r.get(alt_key),(int,float)): point["height_source_value"]=r[alt_key]
            position.append(point)
    # Explain channels that logs do not generally supply.
    motor_fields=[k for k in allfields if "motor" in k.lower() and ("rpm" in k.lower() or "erpm" in k.lower())]
    if not motor_fields:
        diagnoses.append({"title":"Motor RPM unavailable","confidence":"high","status":"unavailable","evidence":[],"reason":"No explicit RPM/eRPM telemetry channel was decoded. Motor output commands are not shaft-speed measurements."})
    # Quality and summary
    def channel_values(tokens):
        ks=[k for k in allfields if any(t in k.lower() for t in tokens)]
        return ks
    gps=channel_values(["lat","lon","gps","latitude","longitude"])
    batt=channel_values(["volt","vbat","curr","battery"])
    vib=channel_values(["vibe","vibration","gyro","accel"])
    voltage_fields=[k for k in allfields if any(x in k.lower() for x in ("voltage_v",".volt","vbat","battery.voltage"))]
    for field in voltage_fields:
        vals=[r[field] for r in clean if isinstance(r.get(field),(int,float))]
        if vals:
            sampled=meta.get("display_sample_stride",1)>1
            diagnoses.append({"title":"Recorded voltage range","confidence":"medium" if sampled else "high","status":"estimated from plot sample" if sampled else "calculated from recorded values","evidence":[field],"reason":f"Minimum {min(vals):g}, maximum {max(vals):g}; range = max − min = {max(vals)-min(vals):g} {'sampled source units' if sampled else 'source units'}. {'Values are from an evenly spaced sample and may miss brief extremes. ' if sampled else ''}No low-voltage threshold or battery cell count was assumed."})
    explicit_vibe=[k for k in allfields if "vibe" in k.lower() or "vibration" in k.lower()]
    if explicit_vibe:
        diagnoses.append({"title":"Vibration telemetry recorded","confidence":"medium","status":"recorded; threshold not applied","evidence":explicit_vibe[:8],"reason":"Explicit vibration-named fields are present. Their units and warning thresholds vary by firmware/configuration; raw values are available in the graphs."})
    elif any("gyro" in k.lower() or "accel" in k.lower() for k in allfields):
        diagnoses.append({"title":"IMU data recorded; vibration health not determined","confidence":"medium","status":"insufficient evidence","evidence":vib[:8],"reason":"Gyroscope/accelerometer channels alone do not establish vibration severity; sample rate, filtering, sensor units, and mounting context are needed."})
    esc_fields=[k for k in allfields if any(x in k.lower() for x in ("esc_error","esc_fault","esc_status","motor_fault"))]
    if esc_fields:
        diagnoses.append({"title":"ESC or motor status fields recorded","confidence":"medium","status":"review source fields","evidence":esc_fields[:8],"reason":"Status fields are present. This milestone preserves them but does not decode firmware-specific bitmasks into a confirmed fault."})
    if not position: diagnoses.append({"title":"GPS position unavailable","confidence":"high","status":"unavailable","evidence":[],"reason":"No recognized numeric latitude/longitude pair was decoded; no takeoff or failure coordinates can be stated."})
    else:
        diagnoses.append({"title":"GPS position recorded","confidence":"high","status":"recorded","evidence":[lat_key,lon_key],"reason":f"{len(position)} numeric position pairs decoded. Display conversion: {'degrees × 1e-7' if scaled_e7 else 'radians to degrees' if is_px4_global else 'source values treated as decimal degrees for recognized field names'}. No GPS accuracy claim is made."})
    summary={"duration_s":round(duration,3),"sample_count":meta.get("decoded_sample_count",len(clean)),"displayed_samples":len(clean),"recorded_fields":len(allfields),"gps_channels":gps[:8],"battery_channels":batt[:8],"vibration_channels":vib[:8],"motor_rpm_channels":motor_fields}
    if not events:
        events=[{"t":0,"title":"Log begins","detail":"First decoded timestamp."},{"t":round(duration,3),"title":"Log ends","detail":"Last decoded timestamp; not necessarily takeoff/landing."}]
    events.sort(key=lambda e: float("inf") if e.get("t") is None else e["t"])
    sampled=meta.get("display_sample_stride",1)>1
    series=compact(clean)
    summary["displayed_samples"]=len(series)
    return {"firmware":firmware,"summary":summary,"fields":allfields,"quality":quality,"missing_channels":missing,"diagnoses":diagnoses,"events":events,"series":series,"position":compact(position),"metadata":meta,"limitations":["Flight duration is logged time span, not independently verified airborne time.","Takeoff/landing and failure timing require reliable state, arming, GPS, or event channels.","Motor output commands are not RPM; RPM is reported only if explicit RPM/eRPM telemetry is recorded.","Position conversion is applied only to recognized common conventions; raw fields are retained.",*( ["Large-log graphs and transient diagnostics use an evenly spaced display sample; exact full-log count, time span, and channel coverage are retained."] if sampled else [] ),"Diagnostics are evidence flags, not causal proof. Sensor placement, calibration, firmware version, and vehicle configuration matter."]}


@app.get("/")
def home(): return FileResponse(ROOT/"static"/"index.html")


def analyze_file(job_id: str, temp: str, suffix: str, head: bytes):
    try:
        if head.startswith(b"ULog"): result=parse_ulog(temp)
        elif head.startswith(b"H Product:") or suffix in (".bbl",".txt",".bfl"): result=parse_blackbox(temp)
        elif suffix==".bin" or head[:2] in (b"\xa3\x95",b"\x95\xa3"): result=parse_dataflash(temp)
        else: raise ValueError("Format not identified. Choose a ULog (.ulg), DataFlash (.bin), or Blackbox (.bbl/.bfl/.txt) file.")
        report=analyze(*result)
        with JOBS_LOCK:
            created=JOBS.get(job_id,{}).get("created_at",time.time())
            JOBS[job_id]={"state":"done","report":report,"created_at":created}
    except Exception as e:
        with JOBS_LOCK:
            created=JOBS.get(job_id,{}).get("created_at",time.time())
            JOBS[job_id]={"state":"error","error":f"Could not decode this log: {str(e)[:500]}","created_at":created}
    finally:
        try: os.unlink(temp)
        except OSError: pass


@app.post("/api/analyze")
async def upload(request: Request, filename: str = "flight-log"):
    content_length=request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length)>MAX_UPLOAD:
        raise HTTPException(413,f"This file is larger than the current {MAX_UPLOAD_MB} MB limit. Set FLIGHTLINE_MAX_UPLOAD_MB to raise the local limit.")
    suffix=Path(filename).suffix.lower()
    job_id=uuid.uuid4().hex
    size=0
    prefix=bytearray()
    temp_path=None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix,delete=False) as f:
            temp_path=f.name
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD: raise HTTPException(413,f"This file is larger than the current {MAX_UPLOAD_MB} MB limit. Set FLIGHTLINE_MAX_UPLOAD_MB to raise the local limit.")
                if len(prefix)<4096: prefix.extend(chunk[:4096-len(prefix)])
                f.write(chunk)
        if not size: raise HTTPException(400,"The selected file is empty.")
        with JOBS_LOCK:
            cutoff=time.time()-3600
            for old_id,old_job in list(JOBS.items()):
                if old_job.get("created_at",0)<cutoff: JOBS.pop(old_id,None)
            JOBS[job_id]={"state":"decoding","filename":Path(filename).name,"created_at":time.time()}
        JOB_POOL.submit(analyze_file,job_id,temp_path,suffix,bytes(prefix))
        return {"job_id":job_id}
    except HTTPException:
        if temp_path:
            try: os.unlink(temp_path)
            except OSError: pass
        raise
    except Exception as e:
        if temp_path:
            try: os.unlink(temp_path)
            except OSError: pass
        raise HTTPException(400,f"Could not receive this file: {str(e)[:300]}")


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        job=JOBS.get(job_id)
        if job and job.get("state") in ("done","error"): JOBS.pop(job_id,None)
    if not job: raise HTTPException(404,"Analysis job not found or expired.")
    return job


app.mount("/static",StaticFiles(directory=ROOT/"static"),name="static")
