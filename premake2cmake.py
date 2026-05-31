#!/usr/bin/env python3
"""
premake2cmake.py - Convert premake5.lua to CMakeLists.txt for CLion
Usage: python premake2cmake.py [input.lua] [output CMakeLists.txt]
"""

import re
import sys
import os
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Project:
    name: str = ""
    kind: str = ""                   # ConsoleApp, WindowedApp, StaticLib, SharedLib
    language: str = "C++"
    cppdialect: str = ""             # C++14, C++17, C++20, …
    cdialect: str = ""
    files: list[str] = field(default_factory=list)
    includedirs: list[str] = field(default_factory=list)
    libdirs: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    targetdir: str = ""
    objdir: str = ""
    pchheader: str = ""
    pchsource: str = ""
    configurations: dict[str, dict] = field(default_factory=dict)  # per-config overrides


@dataclass
class Workspace:
    name: str = ""
    configurations: list[str] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Lua parser (regex-based, handles common premake5 patterns)
# ---------------------------------------------------------------------------

def strip_comments(text: str) -> str:
    """Remove Lua single-line and block comments."""
    text = re.sub(r'--\[\[.*?\]\]', '', text, flags=re.DOTALL)
    text = re.sub(r'--[^\n]*', '', text)
    return text


def extract_string_arg(line: str) -> str:
    """Pull the first quoted string from a line."""
    m = re.search(r'["\']([^"\']*)["\']', line)
    return m.group(1) if m else ""


def extract_table_strings(block: str) -> list[str]:
    """
    Extract all quoted strings from a Lua table literal  { "a", "b", … }
    Also handles multi-line tables.
    """
    return re.findall(r'["\']([^"\']+)["\']', block)


def find_table_block(text: str, pos: int) -> tuple[str, int]:
    """
    Given that text[pos] is '{', extract the full balanced block
    and return (block_content, end_pos).
    """
    depth = 0
    start = pos
    for i in range(pos, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
    return "", len(text)


def parse_lua(source: str) -> Workspace:
    ws = Workspace()
    clean = strip_comments(source)
    lines = clean.splitlines()

    current_project: Optional[Project] = None
    current_config: Optional[str] = None   # inside a filter("configurations:X") block
    i = 0

    def collect_table(keyword: str, line_idx: int) -> tuple[list[str], int]:
        """Find the table for a keyword that may span multiple lines."""
        rest = "\n".join(lines[line_idx:])
        m = re.search(r'\{', rest)
        if not m:
            return [], line_idx
        block, end_offset = find_table_block(rest, m.start())
        items = extract_table_strings(block)
        # advance line_idx past the closing brace
        consumed = rest[:end_offset]
        new_lines_consumed = consumed.count('\n')
        return items, line_idx + new_lines_consumed

    while i < len(lines):
        raw = lines[i].strip()
        lower = raw.lower()

        # --- workspace / solution ---
        if lower.startswith("workspace") or lower.startswith("solution"):
            ws.name = extract_string_arg(raw)

        # --- configurations ---
        elif lower.startswith("configurations"):
            items, i = collect_table("configurations", i)
            ws.configurations = items
            continue

        # --- project ---
        elif lower.startswith("project"):
            p = Project()
            p.name = extract_string_arg(raw)
            current_project = p
            ws.projects.append(p)
            current_config = None

        elif current_project is not None:

            # filter("configurations:Debug") / filter {}
            if lower.startswith("filter"):
                cfg_m = re.search(r'configurations[:\s]*["\']?(\w+)["\']?', raw, re.IGNORECASE)
                if cfg_m:
                    current_config = cfg_m.group(1)
                    if current_config not in current_project.configurations:
                        current_project.configurations[current_config] = {
                            "defines": [], "flags": []
                        }
                elif re.search(r'filter\s*\{\s*\}', raw) or raw == 'filter ""':
                    current_config = None
                else:
                    current_config = None

            elif lower.startswith("kind"):
                current_project.kind = extract_string_arg(raw)

            elif lower.startswith("language"):
                current_project.language = extract_string_arg(raw)

            elif lower.startswith("cppdialect"):
                current_project.cppdialect = extract_string_arg(raw)

            elif lower.startswith("cdialect"):
                current_project.cdialect = extract_string_arg(raw)

            elif lower.startswith("files"):
                items, i = collect_table("files", i)
                current_project.files.extend(items)
                continue

            elif lower.startswith("includedirs"):
                items, i = collect_table("includedirs", i)
                current_project.includedirs.extend(items)
                continue

            elif lower.startswith("libdirs"):
                items, i = collect_table("libdirs", i)
                current_project.libdirs.extend(items)
                continue

            elif lower.startswith("links"):
                items, i = collect_table("links", i)
                if current_config and current_config in current_project.configurations:
                    current_project.configurations[current_config].setdefault("links", []).extend(items)
                else:
                    current_project.links.extend(items)
                continue

            elif lower.startswith("defines"):
                items, i = collect_table("defines", i)
                if current_config and current_config in current_project.configurations:
                    current_project.configurations[current_config]["defines"].extend(items)
                else:
                    current_project.defines.extend(items)
                continue

            elif lower.startswith("targetdir"):
                current_project.targetdir = extract_string_arg(raw)

            elif lower.startswith("objdir"):
                current_project.objdir = extract_string_arg(raw)

            elif lower.startswith("pchheader"):
                current_project.pchheader = extract_string_arg(raw)

            elif lower.startswith("pchsource"):
                current_project.pchsource = extract_string_arg(raw)

        i += 1

    return ws


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PREMAKE_KIND_MAP = {
    "consoleapp":  "EXECUTABLE",
    "windowedapp": "EXECUTABLE",
    "staticlib":   "STATIC",
    "sharedlib":   "SHARED",
    "makefile":    None,
    "none":        None,
}

CPP_STANDARD_MAP = {
    "c++11": "11", "c++14": "14", "c++17": "17", "c++20": "20", "c++23": "23",
    "gnulatest": "23", "latest": "23",
}

C_STANDARD_MAP = {
    "c89": "90", "c90": "90", "c99": "99", "c11": "11", "c17": "17",
    "c18": "17", "gnulatest": "17",
}


def cmake_path(p: str) -> str:
    """Normalise premake token like %{prj.location} to CMake-friendly form."""
    p = p.replace("\\", "/")
    p = re.sub(r'%\{wks\.location\}', '${CMAKE_SOURCE_DIR}', p)
    p = re.sub(r'%\{prj\.location\}', '${CMAKE_CURRENT_SOURCE_DIR}', p)
    p = re.sub(r'%\{cfg\.buildtarget\.directory\}', '${CMAKE_RUNTIME_OUTPUT_DIRECTORY}', p)
    # strip trailing /**  or  /*
    p = re.sub(r'/\*\*?$', '', p)
    return p


def glob_pattern(f: str) -> Optional[str]:
    """
    Convert a premake file glob to a CMake GLOB_RECURSE / GLOB pattern.
    Returns None if it looks like a plain file.
    """
    f = cmake_path(f)
    if "**" in f:
        return f.replace("**", "**")   # kept as-is for GLOB_RECURSE
    if "*" in f:
        return f
    return None


def classify_files(raw_files: list[str]) -> tuple[list[str], list[str]]:
    """Split into glob patterns and plain files."""
    globs, plains = [], []
    for f in raw_files:
        p = glob_pattern(f)
        if p is not None:
            globs.append(cmake_path(f))
        else:
            plains.append(cmake_path(f))
    return globs, plains


# ---------------------------------------------------------------------------
# CMakeLists.txt generator
# ---------------------------------------------------------------------------

def generate_cmake(ws: Workspace) -> str:
    out: list[str] = []
    w = out.append

    cmake_min = "3.20"
    w(f"cmake_minimum_required(VERSION {cmake_min})")
    w(f'project("{ws.name}")')
    w("")

    # Build-type setup
    if ws.configurations:
        cfg_list = ";".join(ws.configurations)
        w(f'# Premake configurations → CMake build types')
        w(f'set(CMAKE_CONFIGURATION_TYPES "{cfg_list}" CACHE STRING "" FORCE)')
        # Guess Debug/Release flags
        for cfg in ws.configurations:
            uname = cfg.upper()
            if "debug" in cfg.lower():
                w(f'set(CMAKE_C_FLAGS_{uname}   "-g -O0")')
                w(f'set(CMAKE_CXX_FLAGS_{uname} "-g -O0")')
            elif "release" in cfg.lower() or "dist" in cfg.lower():
                w(f'set(CMAKE_C_FLAGS_{uname}   "-O3 -DNDEBUG")')
                w(f'set(CMAKE_CXX_FLAGS_{uname} "-O3 -DNDEBUG")')
        w("")

    for proj in ws.projects:
        w("#" + "-" * 70)
        w(f"# Project: {proj.name}")
        w("#" + "-" * 70)
        w("")

        kind_lower = proj.kind.lower()
        cmake_kind = PREMAKE_KIND_MAP.get(kind_lower, "EXECUTABLE")

        if cmake_kind is None:
            w(f"# Skipping project '{proj.name}' (kind={proj.kind} – not supported)")
            w("")
            continue

        # ---- Sources ----
        globs, plains = classify_files(proj.files)
        target_var = f"{proj.name}_SOURCES"

        if globs:
            recurse_globs = [g for g in globs if "**" in g]
            normal_globs  = [g for g in globs if "**" not in g]
            if recurse_globs:
                patterns = "\n    ".join(f'"{g}"' for g in recurse_globs)
                w(f'file(GLOB_RECURSE {target_var}')
                w(f'    {patterns}')
                w(f')')
            if normal_globs:
                patterns = "\n    ".join(f'"{g}"' for g in normal_globs)
                w(f'file(GLOB {target_var}')
                w(f'    {patterns}')
                w(f')')
        if plains:
            w(f'set({target_var}')
            for f in plains:
                w(f'    "{f}"')
            w(')')
        if not globs and not plains:
            w(f'# WARNING: no source files found for {proj.name}')
            w(f'set({target_var} "")')
        w("")

        # ---- Target definition ----
        if cmake_kind == "EXECUTABLE":
            w(f'add_executable({proj.name} ${{{target_var}}})')
        elif cmake_kind == "STATIC":
            w(f'add_library({proj.name} STATIC ${{{target_var}}})')
        elif cmake_kind == "SHARED":
            w(f'add_library({proj.name} SHARED ${{{target_var}}})')
        w("")

        # ---- C/C++ standard ----
        lang = proj.language.lower()
        if lang in ("c++", "cpp") or "c++" in lang:
            std = CPP_STANDARD_MAP.get(proj.cppdialect.lower(), "17")
            w(f'set_target_properties({proj.name} PROPERTIES')
            w(f'    CXX_STANDARD {std}')
            w(f'    CXX_STANDARD_REQUIRED ON')
            w(f'    CXX_EXTENSIONS OFF')
            w( ')')
        elif lang == "c":
            std = C_STANDARD_MAP.get(proj.cdialect.lower(), "11")
            w(f'set_target_properties({proj.name} PROPERTIES')
            w(f'    C_STANDARD {std}')
            w(f'    C_STANDARD_REQUIRED ON')
            w(f'    C_EXTENSIONS OFF')
            w( ')')
        w("")

        # ---- Include directories ----
        if proj.includedirs:
            w(f'target_include_directories({proj.name} PRIVATE')
            for d in proj.includedirs:
                w(f'    "{cmake_path(d)}"')
            w(')')
            w("")

        # ---- Preprocessor defines (global) ----
        if proj.defines:
            w(f'target_compile_definitions({proj.name} PRIVATE')
            for d in proj.defines:
                w(f'    {d}')
            w(')')
            w("")

        # ---- Per-configuration defines & links ----
        for cfg, cfg_data in proj.configurations.items():
            cfg_defines = cfg_data.get("defines", [])
            cfg_links   = cfg_data.get("links", [])
            uname = cfg.upper()
            if cfg_defines:
                w(f'if(CMAKE_BUILD_TYPE STREQUAL "{cfg}")')
                w(f'    target_compile_definitions({proj.name} PRIVATE')
                for d in cfg_defines:
                    w(f'        {d}')
                w( '    )')
                w( 'endif()')
                w("")
            if cfg_links:
                w(f'if(CMAKE_BUILD_TYPE STREQUAL "{cfg}")')
                w(f'    target_link_libraries({proj.name} PRIVATE')
                for l in cfg_links:
                    w(f'        "{l}"')
                w( '    )')
                w( 'endif()')
                w("")

        # ---- Link directories ----
        if proj.libdirs:
            w(f'target_link_directories({proj.name} PRIVATE')
            for d in proj.libdirs:
                w(f'    "{cmake_path(d)}"')
            w(')')
            w("")

        # ---- Link libraries (global) ----
        if proj.links:
            w(f'target_link_libraries({proj.name} PRIVATE')
            for l in proj.links:
                w(f'    "{l}"')
            w(')')
            w("")

        # ---- Output directory ----
        if proj.targetdir:
            td = cmake_path(proj.targetdir)
            w(f'set_target_properties({proj.name} PROPERTIES')
            w(f'    RUNTIME_OUTPUT_DIRECTORY "{td}"')
            w(f'    ARCHIVE_OUTPUT_DIRECTORY "{td}"')
            w(f'    LIBRARY_OUTPUT_DIRECTORY "{td}"')
            w( ')')
            w("")

        # ---- PCH ----
        if proj.pchheader:
            w(f'# Precompiled header')
            w(f'target_precompile_headers({proj.name} PRIVATE "{cmake_path(proj.pchheader)}")')
            w("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    input_file  = sys.argv[1] if len(sys.argv) > 1 else "premake5.lua"
    output_file = sys.argv[2] if len(sys.argv) > 2 else "CMakeLists.txt"

    if not os.path.isfile(input_file):
        print(f"Error: '{input_file}' not found.", file=sys.stderr)
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as f:
        source = f.read()

    workspace = parse_lua(source)
    cmake_text = generate_cmake(workspace)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(cmake_text)

    print(f"✅  Converted '{input_file}' → '{output_file}'")
    print(f"   Workspace : {workspace.name}")
    print(f"   Projects  : {', '.join(p.name for p in workspace.projects) or '(none)'}")


if __name__ == "__main__":
    main()
