import numpy as np, sys
cap=sys.argv[1]; t0=int(open(cap+"/meta.txt").read().split()[1])/1e9
mm=np.memmap(cap+"/iq.cs8",dtype=np.int8).reshape(-1,2)
R=20e6; C=2446e6; n=256
for e in map(float, sys.argv[2:]):
    a=int((e-t0-1.5)*R); b=int((e-t0+2.5)*R)
    x=mm[a:b].astype(np.float32); z=x[:,0]+1j*x[:,1]
    m=len(z)//n
    S=np.abs(np.fft.fftshift(np.fft.fft(z[:m*n].reshape(m,n)*np.hanning(n),axis=1),axes=1))**2
    f=(np.fft.fftshift(np.fft.fftfreq(n,1/R))+C)/1e6
    # 1 MHz sub-bands, smoothed over 2.3 ms (180 blocks) moving average -> find max excess
    k=180
    out=[]
    for fc in np.arange(2437,2456,1.0):
        band=np.abs(f-fc)<0.5
        pb=S[:,band].mean(1)
        ma=np.convolve(pb,np.ones(k)/k,mode="valid")
        med=np.median(pb)
        i=int(np.argmax(ma))
        out.append((fc, round(10*np.log10(ma[i]/med),1), round((a+i*n)/R,4)))
    print("send file t=%.3f"%(e-t0), " ".join(f"{fc:.0f}:{d}@{t}" for fc,d,t in out))
