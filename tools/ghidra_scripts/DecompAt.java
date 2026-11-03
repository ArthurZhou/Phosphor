// DecompAt.java - decompile the function containing each address argument
// Usage: analyzeHeadless ... -postScript DecompAt.java <outFile> <addr> [addr...]

import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

import java.io.File;
import java.io.PrintWriter;

public class DecompAt extends GhidraScript {

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String outPath = args[0];
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);

        try (PrintWriter out = new PrintWriter(new File(outPath), "UTF-8")) {
            for (int i = 1; i < args.length; i++) {
                long t = Long.decode(args[i]);
                Address a = toAddr(t);
                Function f = getFunctionAt(a);
                if (f == null) {
                    f = getFunctionContaining(a);
                }
                if (f == null) {
                    disassemble(a);
                    f = getFunctionAt(a);
                }
                if (f == null) {
                    f = createFunction(a, null);
                }
                if (f == null) {
                    out.printf("/* no function at 0x%08x */%n%n", t);
                    continue;
                }
                out.printf("/* ===== 0x%08x %s (requested 0x%08x) ===== */%n",
                        f.getEntryPoint().getOffset(), f.getName(), t);
                DecompileResults r = di.decompileFunction(f, 120, monitor);
                if (r != null && r.decompileCompleted()) {
                    out.println(r.getDecompiledFunction().getC());
                } else {
                    out.println("/* decompile failed */");
                }
                out.println();
            }
        }
    }
}
