import os
d = r"D:\ESP\.tmp\t1"
os.makedirs(d, exist_ok=True)
names = ["a.whl.metadata","a.whl","a.metadata","a.txt","pyserial-3.5-py2.py3-none-any.whl.metadata",".hidden","a.py","a.zip"]
for n in names:
    p = os.path.join(d, n)
    try:
        with open(p, "wb") as f:
            f.write(b"x")
        print("OK  ", n)
    except Exception as e:
        print("FAIL", n, type(e).__name__, e)
try:
    src = os.path.join(d, "src.tmp")
    with open(src, "wb") as f:
        f.write(b"y")
    os.replace(src, os.path.join(d, "dst.whl"))
    print("OK   os.replace")
except Exception as e:
    print("FAIL os.replace", type(e).__name__, e)
import zipfile
print("zipfile module ok")
