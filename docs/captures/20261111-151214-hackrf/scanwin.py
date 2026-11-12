import numpy as np, sys
cap=sys.argv[1]; t0=int(open(cap+"/meta.txt").read().split()[1])/1e9
mm=np.memmap(cap+"/iq.cs8",dtype=np.int8).reshape(-1,2)
R=20e6; C=2446e6
for e in map(float, sys.argv[2:]):
    a=int((e-t0-2)*R); b=int((e-t0+4)*R)
    x=mm[a:b].astype(np.float32); z=x[:,0]+1j*x[:,1]
    n=256; m=len(z)//n
    S=np.abs(np.fft.fftshift(np.fft.fft(z[:m*n].reshape(m,n),axis=1),axes=1))**2
    f=np.fft.fftshift(np.fft.fftfreq(n,1/R))+C
    # per block (12.8us) power in 2450 +-1 MHz and strongest freq overall
    band=(np.abs(f-2450e6)<1e6)
    pb=10*np.log10(S[:,band].mean(1)+1e-9)
    med=np.median(pb)
    # find runs above med+15 dB lasting > 1 ms
    hot=pb>med+12
    runs=[];st=None
    for i,h in enumerate(hot):
        if h and st is None: st=i
        if not h and st is not None:
            if i-st>40: runs.append((st,i))
            st=None
    print("send %.3f (file t=%.3f): band median %.1f dB, long runs:"%(e,e-t0,med), [(round((a+s*n)/R,4), round((e2-s)*n/R*1e6), round(pb[s:e2].mean()-med,1)) for s,e2 in runs])
    # strongest 2ms-wide events anywhere in 2400-2490? only +-10MHz visible: report top band
    tot=S.sum(0); 
