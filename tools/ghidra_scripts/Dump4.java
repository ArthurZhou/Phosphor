// Dump4.java - 把所有引用设置结构 0x2000A528 / 0x2000A56C 的函数反编译出来
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;

import java.io.File;
import java.io.PrintWriter;
import java.util.Set;
import java.util.TreeSet;

public class Dump4 extends GhidraScript {
    @Override
    public void run() throws Exception {
        String dir = getScriptArgs().length > 0 ? getScriptArgs()[0] : ".";
        long[] targets = {0x2000a528L, 0x2000a56cL, 0x2000001cL, 0x2000025dL};
        Set<Long> funcs = new TreeSet<>();
        PrintWriter idx = new PrintWriter(new File(dir, "settings_index.txt"), "UTF-8");
        for (long t : targets) {
            Address a = toAddr(t);
            ReferenceIterator it = currentProgram.getReferenceManager().getReferencesTo(a);
            while (it.hasNext()) {
                Reference r = it.next();
                Function f = getFunctionContaining(r.getFromAddress());
                if (f != null) {
                    funcs.add(f.getEntryPoint().getOffset());
                    idx.printf("%08x  %s  ref@%08x%n", t, f.getName(), r.getFromAddress().getOffset());
                }
            }
        }
        idx.close();

        PrintWriter out = new PrintWriter(new File(dir, "settings.c"), "UTF-8");
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);
        for (long f : funcs) {
            Function fn = getFunctionAt(toAddr(f));
            if (fn == null) continue;
            out.printf("/* ===== %08x %s (size %d) ===== */%n", f, fn.getName(),
                    fn.getBody().getNumAddresses());
            DecompileResults r = di.decompileFunction(fn, 90, monitor);
            out.println(r != null && r.decompileCompleted() ? r.getDecompiledFunction().getC()
                    : "/* failed */");
            out.println();
        }
        out.close();
        di.dispose();
        println("Dump4 done: " + funcs.size() + " functions");
    }
}
