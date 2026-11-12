import numpy as np, sys
cap=sys.argv[1]; t1=float(sys.argv[2]); t2=float(sys.argv[3])
mm=np.memmap(cap+"/iq.cs8",dtype=np.int8).reshape(-1,2)
R=20e6;C=2446e6;n=200
a=int(t1*R);b=int(t2*R)
x=mm[a:b].astype(np.float32); z=x[:,0]+1j*x[:,1]
m=len(z)//n
S=np.abs(np.fft.fftshift(np.fft.fft(z[:m*n].reshape(m,n)*np.hanning(n),axis=1),axes=1))**2
f=(np.fft.fftshift(np.fft.fftfreq(n,1/R))+C)/1e6
for fc in np.arange(2437,2456,1.0):
    pb=S[:,np.abs(f-fc)<0.5].mean(1); med=np.median(pb)
    hot=pb>med*10
    # runs of >=200 blocks (2 ms) allowing 3-block holes
    runs=[];st=None;miss=0
    for i,h in enumerate(hot):
        if h:
            if st is None: st=i
            miss=0; last=i
        elif st is not None:
            miss+=1
            if miss>3:
                if last-st>=200: runs.append((round(t1+st*n/R,5), round((last-st)*n/R*1e6), round(10*np.log10(pb[st:last].mean()/med),1)))
                st=None
    if runs: print(fc, runs)
