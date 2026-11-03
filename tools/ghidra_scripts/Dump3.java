// Dump3.java - 按 wanted.txt 里列出的函数名批量反编译
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.PrintWriter;

public class Dump3 extends GhidraScript {
    @Override
    public void run() throws Exception {
        String dir = getScriptArgs().length > 0 ? getScriptArgs()[0] : ".";
        File list = new File(dir, "wanted.txt");
        PrintWriter out = new PrintWriter(new File(dir, "wanted_decomp.c"), "UTF-8");
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);
        BufferedReader br = new BufferedReader(new FileReader(list));
        String name;
        int ok = 0;
        while ((name = br.readLine()) != null) {
            name = name.trim();
            if (name.isEmpty()) continue;
            Function f = null;
            for (Function t : currentProgram.getFunctionManager().getFunctions(true)) {
                if (t.getName().equals(name)) { f = t; break; }
            }
            if (f == null) { out.println("/* not found: " + name + " */"); continue; }
            out.printf("/* ===== %s @ %s ===== */%n", name, f.getEntryPoint());
            DecompileResults r = di.decompileFunction(f, 90, monitor);
            out.println(r != null && r.decompileCompleted() ? r.getDecompiledFunction().getC()
                    : "/* failed */");
            out.println();
            ok++;
        }
        br.close();
        out.close();
        di.dispose();
        println("Dump3 done: " + ok);
    }
}
