// Dump.java - Ghidra headless helper for the DSO-153 firmware
// Usage: analyzeHeadless <proj> <name> -process <file> -postScript Dump.java <outDir>

import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;

import java.io.File;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;

public class Dump extends GhidraScript {

    private PrintWriter out;

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String dir = args.length > 0 ? args[0] : ".";
        new File(dir).mkdirs();

        // 1) function map
        out = new PrintWriter(new File(dir, "functions.txt"), "UTF-8");
        FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
        int n = 0;
        while (it.hasNext()) {
            Function f = it.next();
            out.printf("%08x %6d %s%n", f.getEntryPoint().getOffset(),
                    f.getBody().getNumAddresses(), f.getName());
            n++;
        }
        out.close();
        println("functions dumped: " + n);

        // 2) decompile a focused set (main loop + drivers of interest)
        long[] targets = new long[] {
                0x08007000L,             // image base / vector table
                0x08016e74L,             // SystemInit
                0x08016f40L,             // init chain
                0x0801a788L,             // main
                0x08016fc8L,             // settings loader
                0x08009500L, 0x0800b544L, 0x080128f0L, 0x08008ee0L,  // main loop tasks
                0x08012924L, 0x080140c0L,
                0x080180f8L, 0x08018100L, 0x08018274L, 0x08018334L,
                0x080186f8L, 0x080194e8L
        };

        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);

        out = new PrintWriter(new File(dir, "decompiled.c"), "UTF-8");
        for (long t : targets) {
            Address a = toAddr(t);
            Function f = getFunctionAt(a);
            if (f == null) {
                Function c = getFunctionContaining(a);
                if (c == null) {
                    out.printf("/* no function at 0x%08x */%n%n", t);
                    continue;
                }
                f = c;
            }
            out.printf("/* ===== 0x%08x %s ===== */%n", f.getEntryPoint().getOffset(), f.getName());
            DecompileResults r = di.decompileFunction(f, 60, monitor);
            if (r != null && r.decompileCompleted()) {
                out.println(r.getDecompiledFunction().getC());
            } else {
                out.println("/* decompile failed */");
            }
            out.println();
        }
        out.close();

        // 3) references to interesting globals
        out = new PrintWriter(new File(dir, "xrefs.txt"), "UTF-8");
        long[] dataTargets = new long[] {
                0x2000a528L, 0x2000a56cL, 0x2000a580L, 0x2000025aL,
                0x08006000L, 0x08007000L
        };
        for (long t : dataTargets) {
            Address a = toAddr(t);
            out.printf("== 0x%08x%n", t);
            ReferenceIterator refs = currentProgram.getReferenceManager().getReferencesTo(a);
            int k = 0;
            while (refs.hasNext() && k < 40) {
                Reference ref = refs.next();
                Function f = getFunctionContaining(ref.getFromAddress());
                out.printf("   from %08x  %s  in %s%n", ref.getFromAddress().getOffset(),
                        ref.getReferenceType(), f == null ? "?" : f.getName());
                k++;
            }
        }
        out.close();

        di.dispose();
        println("done");
    }
}
