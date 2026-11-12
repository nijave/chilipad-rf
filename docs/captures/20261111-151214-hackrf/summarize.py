import json, numpy as np, sys
rows=[]
for cap in ("cap1","cap2","cap3","cap4","ref_remote"):
    for l in open(cap+"/detail.jsonl"):
        o=json.loads(l); o["cap"]=cap; rows.append(o)
def J(o,k): return (o["junction"] or {}).get(k, np.nan)
fields=[("pwr_db",lambda o:o["pwr_db"]),("snr_db",lambda o:o["snr_db"]),("cfo_demod_kHz",lambda o:o["cfo_demod"]/1e3),("cfo_fit_kHz",lambda o:o["cfo_hz"]/1e3),("cfo_ppm",lambda o:o["cfo_ppm"]),
 ("dev_demod_kHz",lambda o:o["dev_demod"]/1e3),("dev_fit_kHz",lambda o:o["dev_hz"]/1e3),("dur_us",lambda o:o["dur_us"]),("on50_us",lambda o:o["on_us"]),
 ("rise_us",lambda o:o["rise_us"]),("fall_us",lambda o:o["fall_us"]),("lead_us",lambda o:o["lead_us"]),("lead_f_kHz",lambda o:o["lead_f_hz"]/1e3),("alt_bits",lambda o:o["alt_bits"]),
 ("drift_Hz_per_ms",lambda o:o["cfo_slope_hz_per_ms"]),("cfo_win_std_Hz",lambda o:o["cfo_win_std_hz"]),("first100us_minus_mean_Hz",lambda o:o["cfo_first_minus_mean_hz"]),
 ("junc_phase_deg",lambda o:J(o,"phase_jump_deg")),("junc_maxres_kHz",lambda o:J(o,"max_res_khz")),("junc_rmsres_kHz",lambda o:J(o,"rms_res_khz")),("ref_rmsres_kHz",lambda o:J(o,"ref_rms_res_khz")),("junc_minamp",lambda o:J(o,"min_amp_rel")),("ref_minamp",lambda o:J(o,"ref_min_amp_rel")),
 ("obw99_kHz",lambda o:o["obw99_hz"]/1e3),("bw20_kHz",lambda o:o["bw20_hz"]/1e3),("psd-1.0",lambda o:o["psd"]["-1.0"]),("psd+1.0",lambda o:o["psd"]["+1.0"]),("psd-1.5",lambda o:o["psd"]["-1.5"]),("psd+1.5",lambda o:o["psd"]["+1.5"]),("psd-2.0",lambda o:o["psd"]["-2.0"]),("psd+2.0",lambda o:o["psd"]["+2.0"]),("errs",lambda o:o["errs"])]
groups={}
for o in rows:
    clean = o["errs"]<=2 and o["cap"]!="cap4"
    key=(o["device"], "hi" if o["cap"] in ("cap2","cap3") else o["cap"])
    if o["device"]=="remote": key=("remote","ref")
    if o["cap"] in ("cap2","cap3") and o["errs"]>2: key=(o["device"],"hi-interfered")
    groups.setdefault(key,[]).append(o)
for k,g in sorted(groups.items()):
    print("==",k,"n=",len(g), [ (x["cap"],round(x["t"],3)) for x in g])
    for name,fn in fields:
        v=np.array([fn(o) for o in g],float)
        print(f"   {name:26s} mean {np.nanmean(v):10.2f}  sd {np.nanstd(v):8.2f}  min {np.nanmin(v):10.2f} max {np.nanmax(v):10.2f}")
# PSD average comparison, cap2+cap3 clean
import collections
P=collections.defaultdict(list)
for o in rows:
    if o["cap"] in ("cap2","cap3") and o["errs"]==0: P[o["device"]].append(np.array(o["psd_raw"]))
fr=np.fft.fftshift(np.fft.fftfreq(1024,1/20e6))
for d,v in P.items():
    m=10*np.log10(np.mean([10**(x/10) for x in v],axis=0))
    P[d]=m
PO=P["ORIGINAL"]; PR=P["RAYTAC"]; diff=PO-PR
print("PSD (dB rel peak), cap2+cap3 clean bursts, offset MHz: ORIGINAL / RAYTAC / diff")
for f0 in (-5,-4,-3,-2.5,-2,-1.5,-1.25,-1,-0.75,-0.5,-0.25,0,0.25,0.5,0.75,1,1.25,1.5,2,2.5,3,4,5,6,8):
    j=np.abs(fr-f0*1e6)<0.1e6
    print(f"  {f0:+6.2f}  {PO[j].mean():7.1f} {PR[j].mean():7.1f} {diff[j].mean():+6.1f}")
