@echo off
set JAVA_HOME=D:\dev\tools\jdk-21.0.12.1+1
set GHIDRA=D:\dev\tools\ghidra_12.2_PUBLIC
call "%GHIDRA%\support\analyzeHeadless.bat" "D:\dev\dso153\ghidra_proj" DSO153 -import "D:\dev\dso153\APP_DSO153_V1.6_20240427.bin" -processor ARM:LE:32:Cortex -loader BinaryLoader -loader-baseAddr 0x08007000 -scriptPath "D:\dev\dso153\tools\ghidra_scripts" -postScript Dump.java "D:\dev\dso153\tools\build\ghidra_out" > "D:\dev\dso153\tools\build\ghidra.log" 2>&1
