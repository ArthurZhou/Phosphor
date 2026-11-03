// Dump2.java - 第二轮：定位 ADC/DMA 采集路径与 RAM 缓冲区
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.lang.OperandType;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.scalar.Scalar;

import java.io.File;
import java.io.PrintWriter;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;

public class Dump2 extends GhidraScript {

    @Override
    public void run() throws Exception {
        String dir = getScriptArgs().length > 0 ? getScriptArgs()[0] : ".";
        PrintWriter w = new PrintWriter(new File(dir, "periph.txt"), "UTF-8");

        Map<Long, Set<Long>> hits = new TreeMap<>();
        Map<Long, Integer> ramCounts = new TreeMap<>();

        InstructionIterator it = currentProgram.getListing().getInstructions(true);
        while (it.hasNext()) {
            Instruction ins = it.next();
            for (int i = 0; i < ins.getNumOperands(); i++) {
                if ((ins.getOperandType(i) & OperandType.SCALAR) == 0) continue;
                if (ins.getOpObjects(i).length == 0) continue;
                if (!(ins.getOpObjects(i)[0] instanceof Scalar)) continue;
                Scalar s = (Scalar) ins.getOpObjects(i)[0];
                long v = s.getUnsignedValue();
                boolean periph = (v >= 0x40000000L && v < 0x50000000L)
                        || (v >= 0x50000000L && v < 0x50001000L);
                if (periph) {
                    hits.computeIfAbsent(v, k -> new TreeSet<>()).add(ins.getAddress().getOffset());
                }
                if (v >= 0x20000000L && v < 0x2000c000L) {
                    ramCounts.merge(v, 1, Integer::sum);
                }
            }
        }

        w.println("### 外设寄存器常量（操作数中是立即数的） ###");
        for (Map.Entry<Long, Set<Long>> e : hits.entrySet()) {
            long v = e.getKey();
            w.printf("%08x  x%d", v, e.getValue().size());
            int n = 0;
            for (long a : e.getValue()) {
                if (n++ >= 5) break;
                Function f = getFunctionContaining(toAddr(a));
                w.printf("  @%08x(%s)", a, f == null ? "?" : f.getName());
            }
            w.println();
        }

        w.println();
        w.println("### RAM 地址引用计数 top 40 ###");
        ramCounts.entrySet().stream()
                .sorted((a, b) -> b.getValue() - a.getValue())
                .limit(40)
                .forEach(e -> w.printf("%08x  %d%n", e.getKey(), e.getValue()));
        w.close();

        // 反编译 ADC/DMA/定时器 相关函数
        PrintWriter d = new PrintWriter(new File(dir, "acq.c"), "UTF-8");
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);
        Set<Long> funcs = new TreeSet<>();
        for (Map.Entry<Long, Set<Long>> e : hits.entrySet()) {
            long v = e.getKey();
            if (!((v >= 0x40012000L && v < 0x40012200L)
                    || (v >= 0x40026000L && v < 0x40026500L)
                    || (v >= 0x40010000L && v < 0x40011000L)))
                continue;
            for (long a : e.getValue()) {
                Function f = getFunctionContaining(toAddr(a));
                if (f != null) funcs.add(f.getEntryPoint().getOffset());
            }
        }
        for (long f : funcs) {
            Function fn = getFunctionAt(toAddr(f));
            if (fn == null) continue;
            d.printf("/* ===== %08x %s ===== */%n", f, fn.getName());
            DecompileResults r = di.decompileFunction(fn, 60, monitor);
            d.println(r != null && r.decompileCompleted() ? r.getDecompiledFunction().getC()
                    : "/* failed */");
            d.println();
        }
        d.close();
        di.dispose();
        println("Dump2 done, functions: " + funcs.size());
    }
}
