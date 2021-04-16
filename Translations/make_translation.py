#!/usr/bin/env python3

import argparse
import functools
import json
import logging
import os
import pickle
import re
import subprocess
import sys
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, TextIO, Tuple, Union
from dataclasses import dataclass

from bdflib import reader as bdfreader
from bdflib.model import Font, Glyph

import font_tables
import lzfx

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

HERE = Path(__file__).resolve().parent


@functools.lru_cache(maxsize=None)
def cjk_font() -> Font:
    with open(os.path.join(HERE, "wqy-bitmapsong/wenquanyi_9pt.bdf"), "rb") as f:
        return bdfreader.read_bdf(f)


# Loading a single JSON file
def load_json(filename: str, skip_first_line: bool) -> dict:
    with open(filename) as f:
        if skip_first_line:
            f.readline()
        return json.loads(f.read())


def read_translation(json_root: Union[str, Path], lang_code: str) -> dict:
    filename = f"translation_{lang_code}.json"

    file_with_path = os.path.join(json_root, filename)

    try:
        lang = load_json(file_with_path, skip_first_line=False)
    except json.decoder.JSONDecodeError as e:
        logging.error(f"Failed to decode {filename}")
        logging.exception(str(e))
        sys.exit(2)

    validate_langcode_matches_content(filename, lang)

    return lang


def validate_langcode_matches_content(filename: str, content: dict) -> None:
    # Extract lang code from file name
    lang_code = filename[12:-5].upper()
    # ...and the one specified in the JSON file...
    try:
        lang_code_from_json = content["languageCode"]
    except KeyError:
        lang_code_from_json = "(missing)"

    # ...cause they should be the same!
    if lang_code != lang_code_from_json:
        raise ValueError(
            f"Invalid languageCode {lang_code_from_json} in file {filename}"
        )


def write_start(f: TextIO):
    f.write(
        "// WARNING: THIS FILE WAS AUTO GENERATED BY make_translation.py. PLEASE DO NOT EDIT.\n"
    )
    f.write("\n")
    f.write('#include "Translation.h"\n')


def get_constants(build_version: str) -> List[Tuple[str, str]]:
    # Extra constants that are used in the firmware that are shared across all languages
    return [
        ("SymbolPlus", "+"),
        ("SymbolMinus", "-"),
        ("SymbolSpace", " "),
        ("SymbolDot", "."),
        ("SymbolDegC", "C"),
        ("SymbolDegF", "F"),
        ("SymbolMinutes", "M"),
        ("SymbolSeconds", "S"),
        ("SymbolWatts", "W"),
        ("SymbolVolts", "V"),
        ("SymbolDC", "DC"),
        ("SymbolCellCount", "S"),
        ("SymbolVersionNumber", build_version),
    ]


def get_debug_menu() -> List[str]:
    return [
        datetime.today().strftime("%d-%m-%y"),
        "HW G ",
        "HW M ",
        "HW P ",
        "Time ",
        "Move ",
        "RTip ",
        "CTip ",
        "CHan ",
        "Vin  ",
        "PCB  ",
        "PWR  ",
        "Max  ",
    ]


def get_letter_counts(defs: dict, lang: dict, build_version: str) -> List[str]:
    text_list = []
    # iterate over all strings
    obj = lang["menuOptions"]
    for mod in defs["menuOptions"]:
        eid = mod["id"]
        text_list.append(obj[eid]["desc"])

    obj = lang["messages"]
    for mod in defs["messages"]:
        eid = mod["id"]
        if eid not in obj:
            text_list.append(mod["default"])
        else:
            text_list.append(obj[eid])

    obj = lang["messagesWarn"]
    for mod in defs["messagesWarn"]:
        eid = mod["id"]
        if isinstance(obj[eid], list):
            text_list.append(obj[eid][0])
            text_list.append(obj[eid][1])
        else:
            text_list.append(obj[eid])

    obj = lang["characters"]

    for mod in defs["characters"]:
        eid = mod["id"]
        text_list.append(obj[eid])

    obj = lang["menuOptions"]
    for mod in defs["menuOptions"]:
        eid = mod["id"]
        if isinstance(obj[eid]["text2"], list):
            text_list.append(obj[eid]["text2"][0])
            text_list.append(obj[eid]["text2"][1])
        else:
            text_list.append(obj[eid]["text2"])

    obj = lang["menuGroups"]
    for mod in defs["menuGroups"]:
        eid = mod["id"]
        if isinstance(obj[eid]["text2"], list):
            text_list.append(obj[eid]["text2"][0])
            text_list.append(obj[eid]["text2"][1])
        else:
            text_list.append(obj[eid]["text2"])

    obj = lang["menuGroups"]
    for mod in defs["menuGroups"]:
        eid = mod["id"]
        text_list.append(obj[eid]["desc"])
    constants = get_constants(build_version)
    for x in constants:
        text_list.append(x[1])
    text_list.extend(get_debug_menu())

    # collapse all strings down into the composite letters and store totals for these

    symbol_counts: dict[str, int] = {}
    for line in text_list:
        line = line.replace("\n", "").replace("\r", "")
        line = line.replace("\\n", "").replace("\\r", "")
        if line:
            for letter in line:
                symbol_counts[letter] = symbol_counts.get(letter, 0) + 1
    # swap to Big -> little sort order
    symbols_by_occurrence = [
        x[0] for x in sorted(symbol_counts.items(), key=lambda kv: (kv[1], kv[0]))
    ]
    symbols_by_occurrence.reverse()
    return symbols_by_occurrence


def get_cjk_glyph(sym: str) -> bytes:
    glyph: Glyph = cjk_font()[ord(sym)]

    data = glyph.data
    src_left, src_bottom, src_w, src_h = glyph.get_bounding_box()
    dst_w = 12
    dst_h = 16

    # The source data is a per-row list of ints. The first item is the bottom-
    # most row. For each row, the LSB is the right-most pixel.
    # Here, (x, y) is the coordinates with origin at the top-left.
    def get_cell(x: int, y: int) -> bool:
        # Adjust x coordinates by actual bounding box.
        adj_x = x - src_left
        if adj_x < 0 or adj_x >= src_w:
            return False
        # Adjust y coordinates by actual bounding box, then place the glyph
        # baseline 3px above the bottom edge to make it centre-ish.
        # This metric is optimized for WenQuanYi Bitmap Song 9pt and assumes
        # each glyph is to be placed in a 12x12px box.
        adj_y = y - (dst_h - src_h - src_bottom - 3)
        if adj_y < 0 or adj_y >= src_h:
            return False
        if data[src_h - adj_y - 1] & (1 << (src_w - adj_x - 1)):
            return True
        else:
            return False

    # A glyph in the font table is divided into upper and lower parts, each by
    # 8px high. Each byte represents half if a column, with the LSB being the
    # top-most pixel. The data goes from the left-most to the right-most column
    # of the top half, then from the left-most to the right-most column of the
    # bottom half.
    bs = bytearray()
    for block in range(2):
        for c in range(dst_w):
            b = 0
            for r in range(8):
                if get_cell(c, r + 8 * block):
                    b |= 0x01 << r
            bs.append(b)
    return bytes(bs)


def get_bytes_from_font_index(index: int) -> bytes:
    """
    Converts the font table index into its corresponding bytes
    """

    # We want to be able to use more than 254 symbols (excluding \x00 null
    # terminator and \x01 new-line) in the font table but without making all
    # the chars take 2 bytes. To do this, we use \xF1 to \xFF as lead bytes
    # to designate double-byte chars, and leave the remaining as single-byte
    # chars.
    #
    # For the sake of sanity, \x00 always means the end of string, so we skip
    # \xF1\x00 and others in the mapping.
    #
    # Mapping example:
    #
    # 0x02 => 2
    # 0x03 => 3
    # ...
    # 0xEF => 239
    # 0xF0 => 240
    # 0xF1 0x01 => 1 * 0xFF - 15 + 1 = 241
    # 0xF1 0x02 => 1 * 0xFF - 15 + 2 = 242
    # ...
    # 0xF1 0xFF => 1 * 0xFF - 15 + 255 = 495
    # 0xF2 0x01 => 2 * 0xFF - 15 + 1 = 496
    # ...
    # 0xF2 0xFF => 2 * 0xFF - 15 + 255 = 750
    # 0xF3 0x01 => 3 * 0xFF - 15 + 1 = 751
    # ...
    # 0xFF 0xFF => 15 * 0xFF - 15 + 255 = 4065

    if index < 0:
        raise ValueError("index must be positive")
    page = (index + 0x0E) // 0xFF
    if page > 0x0F:
        raise ValueError("page value out of range")
    if page == 0:
        return bytes([index])
    else:
        # Into extended range
        # Leader is 0xFz where z is the page number
        # Following char is the remainder
        leader = page + 0xF0
        value = ((index + 0x0E) % 0xFF) + 0x01

        if leader > 0xFF or value > 0xFF:
            raise ValueError("value is out of range")
        return bytes([leader, value])


def bytes_to_escaped(b: bytes) -> str:
    return "".join((f"\\x{i:02X}" for i in b))


def bytes_to_c_hex(b: bytes) -> str:
    return ", ".join((f"0x{i:02X}" for i in b)) + ","


@dataclass
class FontMap:
    font12: Dict[str, bytes]
    font06: Dict[str, Optional[bytes]]


@dataclass
class FontMapsPerFont:
    font12_maps: Dict[str, Dict[str, bytes]]
    font06_maps: Dict[str, Dict[str, Optional[bytes]]]
    sym_lists: Dict[str, List[str]]


def get_font_map_per_font(text_list: List[str], fonts: List[str]) -> FontMapsPerFont:
    pending_sym_set = set(text_list)
    if len(pending_sym_set) != len(text_list):
        raise ValueError("`text_list` contains duplicated symbols")

    if fonts[0] != font_tables.NAME_ASCII_BASIC:
        raise ValueError(
            f'First item in `fonts` must be "{font_tables.NAME_ASCII_BASIC}"'
        )

    total_symbol_count = len(text_list)
    # \x00 is for NULL termination and \x01 is for newline, so the maximum
    # number of symbols allowed is as follow (see also the comments in
    # `get_bytes_from_font_index`):
    if total_symbol_count > (0x10 * 0xFF - 15) - 2:  # 4063
        raise ValueError(
            f"Error, too many used symbols for this version (total {total_symbol_count})"
        )

    logging.info(f"Generating fonts for {total_symbol_count} symbols")

    # Collect font bitmaps by the defined font order:
    font12_maps: Dict[str, Dict[str, bytes]] = {}
    font06_maps: Dict[str, Dict[str, Optional[bytes]]] = {}
    sym_lists: Dict[str, List[str]] = {}
    for font in fonts:
        font12_maps[font] = {}
        font12_map = font12_maps[font]
        font06_maps[font] = {}
        font06_map = font06_maps[font]
        sym_lists[font] = []
        sym_list = sym_lists[font]

        if len(pending_sym_set) == 0:
            logging.warning(
                f"Font {font} not used because all symbols already have font bitmaps"
            )
            continue

        if font == font_tables.NAME_CJK:
            is_cjk = True
        else:
            is_cjk = False
            font12: Dict[str, bytes]
            font06: Dict[str, bytes]
            font12, font06 = font_tables.get_font_maps_for_name(font)

        for sym in text_list:
            if sym not in pending_sym_set:
                continue
            if is_cjk:
                font12_line = get_cjk_glyph(sym)
                if font12_line is None:
                    continue
                font06_line = None
            else:
                try:
                    font12_line = font12[sym]
                    font06_line = font06[sym]
                except KeyError:
                    continue
            font12_map[sym] = font12_line
            font06_map[sym] = font06_line
            sym_list.append(sym)
            pending_sym_set.remove(sym)

        if len(sym_list) == 0:
            logging.warning(f"Font {font} not used by any symbols on the list")
    if len(pending_sym_set) > 0:
        raise KeyError(f"Symbols not found in specified fonts: {pending_sym_set}")

    return FontMapsPerFont(font12_maps, font06_maps, sym_lists)


def get_font_map_and_table(
    text_list: List[str], fonts: List[str]
) -> Tuple[List[str], FontMap, Dict[str, bytes]]:
    # the text list is sorted
    # allocate out these in their order as number codes
    symbol_map: Dict[str, bytes] = {"\n": bytes([1])}
    index = 2  # start at 2, as 0= null terminator,1 = new line
    forced_first_symbols = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]

    # We enforce that numbers come first.
    text_list = forced_first_symbols + [
        x for x in text_list if x not in forced_first_symbols
    ]

    font_maps = get_font_map_per_font(text_list, fonts)
    font12_maps = font_maps.font12_maps
    font06_maps = font_maps.font06_maps

    # Build the full font maps
    font12_map = {}
    font06_map = {}
    for font in fonts:
        font12_map.update(font12_maps[font])
        font06_map.update(font06_maps[font])

    # Collect all symbols by the original symbol order, but also making sure
    # all symbols with only large font must be placed after all symbols with
    # both small and large fonts
    sym_list_both_fonts = []
    sym_list_large_only = []
    for sym in text_list:
        if font06_map[sym] is None:
            sym_list_large_only.append(sym)
        else:
            sym_list_both_fonts.append(sym)
    sym_list = sym_list_both_fonts + sym_list_large_only

    # Assign symbol bytes by font index
    for index, sym in enumerate(sym_list, index):
        assert sym not in symbol_map
        symbol_map[sym] = get_bytes_from_font_index(index)

    return sym_list, FontMap(font12_map, font06_map), symbol_map


def make_font_table_cpp(
    sym_list: List[str], font_map: FontMap, symbol_map: Dict[str, bytes]
) -> str:
    output_table = make_font_table_12_cpp(sym_list, font_map, symbol_map)
    output_table += make_font_table_06_cpp(sym_list, font_map, symbol_map)
    return output_table


def make_font_table_12_cpp(
    sym_list: List[str], font_map: FontMap, symbol_map: Dict[str, bytes]
) -> str:
    output_table = "const uint8_t USER_FONT_12[] = {\n"
    for sym in sym_list:
        output_table += f"{bytes_to_c_hex(font_map.font12[sym])}//{bytes_to_escaped(symbol_map[sym])} -> {sym}\n"
    output_table += "};\n"
    return output_table


def make_font_table_06_cpp(
    sym_list: List[str], font_map: FontMap, symbol_map: Dict[str, bytes]
) -> str:
    output_table = "const uint8_t USER_FONT_6x8[] = {\n"
    for sym in sym_list:
        font_bytes = font_map.font06[sym]
        if font_bytes:
            font_line = bytes_to_c_hex(font_bytes)
        else:
            font_line = "//                                 "  # placeholder
        output_table += f"{font_line}//{bytes_to_escaped(symbol_map[sym])} -> {sym}\n"
    output_table += "};\n"
    return output_table


def convert_string_bytes(symbol_conversion_table: Dict[str, bytes], text: str) -> bytes:
    # convert all of the symbols from the string into bytes for their content
    output_string = b""
    for c in text.replace("\\r", "").replace("\\n", "\n"):
        if c not in symbol_conversion_table:
            logging.error(f"Missing font definition for {c}")
            sys.exit(1)
        else:
            output_string += symbol_conversion_table[c]
    return output_string


def convert_string(symbol_conversion_table: Dict[str, bytes], text: str) -> str:
    # convert all of the symbols from the string into escapes for their content
    return bytes_to_escaped(convert_string_bytes(symbol_conversion_table, text))


def escape(string: str) -> str:
    return json.dumps(string, ensure_ascii=False)


def write_bytes_as_c_array(
    f: TextIO, name: str, data: bytes, indent: int = 2, bytes_per_line: int = 16
) -> None:
    f.write(f"const uint8_t {name}[] = {{\n")
    for i in range(0, len(data), bytes_per_line):
        f.write(" " * indent)
        f.write(", ".join((f"0x{b:02X}" for b in data[i : i + bytes_per_line])))
        f.write(",\n")
    f.write(f"}}; // {name}\n\n")


@dataclass
class LanguageData:
    lang: dict
    defs: dict
    build_version: str
    sym_list: List[str]
    font_map: FontMap
    symbol_conversion_table: Dict[str, bytes]


def prepare_language(lang: dict, defs: dict, build_version: str) -> LanguageData:
    language_code: str = lang["languageCode"]
    logging.info(f"Preparing language data for {language_code}")
    # Iterate over all of the text to build up the symbols & counts
    text_list = get_letter_counts(defs, lang, build_version)
    # From the letter counts, need to make a symbol translator & write out the font
    fonts = lang["fonts"]
    sym_list, font_map, symbol_conversion_table = get_font_map_and_table(
        text_list, fonts
    )
    return LanguageData(
        lang, defs, build_version, sym_list, font_map, symbol_conversion_table
    )


def write_language(
    data: LanguageData,
    f: TextIO,
    strings_bin: Optional[bytes] = None,
    compress_font: bool = False,
) -> None:
    lang = data.lang
    defs = data.defs
    build_version = data.build_version
    sym_list = data.sym_list
    font_map = data.font_map
    symbol_conversion_table = data.symbol_conversion_table

    language_code: str = lang["languageCode"]
    logging.info(f"Generating block for {language_code}")

    try:
        lang_name = lang["languageLocalName"]
    except KeyError:
        lang_name = language_code

    if strings_bin or compress_font:
        f.write('#include "lzfx.h"\n')

    f.write(f"\n// ---- {lang_name} ----\n\n")

    if not compress_font:
        font_table_text = make_font_table_cpp(
            sym_list, font_map, symbol_conversion_table
        )
        f.write(font_table_text)
    else:
        font12_uncompressed = bytearray()
        for sym in sym_list:
            font12_uncompressed.extend(font_map.font12[sym])
        font12_compressed = lzfx.compress(bytes(font12_uncompressed))
        logging.info(
            f"Font table 12x16 compressed from {len(font12_uncompressed)} to {len(font12_compressed)} bytes (ratio {len(font12_compressed) / len(font12_uncompressed):.3})"
        )
        write_bytes_as_c_array(f, "font_12x16_lzfx", font12_compressed)
        font_table_text = make_font_table_06_cpp(
            sym_list, font_map, symbol_conversion_table
        )
        f.write(font_table_text)

    f.write(f"\n// ---- {lang_name} ----\n\n")

    translation_common_text = get_translation_common_text(
        defs, symbol_conversion_table, build_version
    )
    f.write(translation_common_text)
    f.write(
        f"const bool HasFahrenheit = {('true' if lang.get('tempUnitFahrenheit', True) else 'false')};\n\n"
    )

    if not compress_font:
        f.write("extern const uint8_t *const Font_12x16 = USER_FONT_12;\n")
    else:
        f.write(
            f"static uint8_t font_out_buffer[{len(font12_uncompressed)}];\n\n"
            "extern const uint8_t *const Font_12x16 = font_out_buffer;\n"
        )
    f.write("extern const uint8_t *const Font_6x8 = USER_FONT_6x8;\n\n")

    if not strings_bin:
        translation_strings_and_indices_text = get_translation_strings_and_indices_text(
            lang, defs, symbol_conversion_table
        )
        f.write(translation_strings_and_indices_text)
        f.write(
            "const TranslationIndexTable *const Tr = &TranslationIndices;\n"
            "const char *const TranslationStrings = TranslationStringsData;\n\n"
        )
    else:
        compressed = lzfx.compress(strings_bin)
        logging.info(
            f"Strings compressed from {len(strings_bin)} to {len(compressed)} bytes (ratio {len(compressed) / len(strings_bin):.3})"
        )
        write_bytes_as_c_array(f, "translation_data_lzfx", compressed)
        f.write(
            f"static uint8_t translation_data_out_buffer[{len(strings_bin)}] __attribute__((__aligned__(2)));\n\n"
            "const TranslationIndexTable *const Tr = reinterpret_cast<const TranslationIndexTable *>(translation_data_out_buffer);\n"
            "const char *const TranslationStrings = reinterpret_cast<const char *>(translation_data_out_buffer) + sizeof(TranslationIndexTable);\n\n"
        )

    if not strings_bin and not compress_font:
        f.write("void prepareTranslations() {}\n\n")
    else:
        f.write("void prepareTranslations() {\n" "  unsigned int outsize;\n")
        if compress_font:
            f.write(
                "  outsize = sizeof(font_out_buffer);\n"
                "  lzfx_decompress(font_12x16_lzfx, sizeof(font_12x16_lzfx), font_out_buffer, &outsize);\n"
            )
        if strings_bin:
            f.write(
                "  outsize = sizeof(translation_data_out_buffer);\n"
                "  lzfx_decompress(translation_data_lzfx, sizeof(translation_data_lzfx), translation_data_out_buffer, &outsize);\n"
            )
        f.write("}\n\n")

    sanity_checks_text = get_translation_sanity_checks_text(defs)
    f.write(sanity_checks_text)


def get_translation_common_text(
    defs: dict, symbol_conversion_table: Dict[str, bytes], build_version
) -> str:
    translation_common_text = ""

    # Write out firmware constant options
    constants = get_constants(build_version)
    for x in constants:
        translation_common_text += f'const char* {x[0]} = "{convert_string(symbol_conversion_table, x[1])}";//{x[1]} \n'
    translation_common_text += "\n"

    # Debug Menu
    translation_common_text += "const char* DebugMenu[] = {\n"

    for c in get_debug_menu():
        translation_common_text += (
            f'\t "{convert_string(symbol_conversion_table, c)}",//{c} \n'
        )
    translation_common_text += "};\n\n"
    return translation_common_text


@dataclass
class TranslationItem:
    info: str
    str_index: int


def get_translation_strings_and_indices_text(
    lang: dict, defs: dict, symbol_conversion_table: Dict[str, bytes]
) -> str:
    str_table: List[str] = []
    str_group_messages: List[TranslationItem] = []
    str_group_messageswarn: List[TranslationItem] = []
    str_group_characters: List[TranslationItem] = []
    str_group_settingdesc: List[TranslationItem] = []
    str_group_settingshortnames: List[TranslationItem] = []
    str_group_settingmenuentries: List[TranslationItem] = []
    str_group_settingmenuentriesdesc: List[TranslationItem] = []

    eid: str

    # ----- Reading SettingsDescriptions
    obj = lang["menuOptions"]

    for index, mod in enumerate(defs["menuOptions"]):
        eid = mod["id"]
        str_group_settingdesc.append(
            TranslationItem(f"[{index:02d}] {eid}", len(str_table))
        )
        str_table.append(obj[eid]["desc"])

    # ----- Reading Message strings

    obj = lang["messages"]

    for mod in defs["messages"]:
        eid = mod["id"]
        source_text = ""
        if "default" in mod:
            source_text = mod["default"]
        if eid in obj:
            source_text = obj[eid]
        str_group_messages.append(TranslationItem(eid, len(str_table)))
        str_table.append(source_text)

    obj = lang["messagesWarn"]

    for mod in defs["messagesWarn"]:
        eid = mod["id"]
        if isinstance(obj[eid], list):
            if not obj[eid][1]:
                source_text = obj[eid][0]
            else:
                source_text = obj[eid][0] + "\n" + obj[eid][1]
        else:
            source_text = "\n" + obj[eid]
        str_group_messageswarn.append(TranslationItem(eid, len(str_table)))
        str_table.append(source_text)

    # ----- Reading Characters

    obj = lang["characters"]

    for mod in defs["characters"]:
        eid = mod["id"]
        str_group_characters.append(TranslationItem(eid, len(str_table)))
        str_table.append(obj[eid])

    # ----- Reading SettingsDescriptions
    obj = lang["menuOptions"]

    for index, mod in enumerate(defs["menuOptions"]):
        eid = mod["id"]
        if isinstance(obj[eid]["text2"], list):
            if not obj[eid]["text2"][1]:
                source_text = obj[eid]["text2"][0]
            else:
                source_text = obj[eid]["text2"][0] + "\n" + obj[eid]["text2"][1]
        else:
            source_text = "\n" + obj[eid]["text2"]
        str_group_settingshortnames.append(
            TranslationItem(f"[{index:02d}] {eid}", len(str_table))
        )
        str_table.append(source_text)

    # ----- Reading Menu Groups
    obj = lang["menuGroups"]

    for index, mod in enumerate(defs["menuGroups"]):
        eid = mod["id"]
        if isinstance(obj[eid]["text2"], list):
            if not obj[eid]["text2"][1]:
                source_text = obj[eid]["text2"][0]
            else:
                source_text = obj[eid]["text2"][0] + "\n" + obj[eid]["text2"][1]
        else:
            source_text = "\n" + obj[eid]["text2"]
        str_group_settingmenuentries.append(
            TranslationItem(f"[{index:02d}] {eid}", len(str_table))
        )
        str_table.append(source_text)

    # ----- Reading Menu Groups Descriptions
    obj = lang["menuGroups"]

    for index, mod in enumerate(defs["menuGroups"]):
        eid = mod["id"]
        str_group_settingmenuentriesdesc.append(
            TranslationItem(f"[{index:02d}] {eid}", len(str_table))
        )
        str_table.append(obj[eid]["desc"])

    @dataclass
    class RemappedTranslationItem:
        str_index: int
        str_start_offset: int = 0

    # ----- Perform suffix merging optimization:
    #
    # We sort the backward strings so that strings with the same suffix will
    # be next to each other, e.g.:
    #   "ef\0",
    #   "cdef\0",
    #   "abcdef\0",
    backward_sorted_table: List[Tuple[int, str, bytes]] = sorted(
        (
            (i, s, bytes(reversed(convert_string_bytes(symbol_conversion_table, s))))
            for i, s in enumerate(str_table)
        ),
        key=lambda x: x[2],
    )
    str_remapping: List[Optional[RemappedTranslationItem]] = [None] * len(str_table)
    for i, (str_index, source_str, converted) in enumerate(backward_sorted_table[:-1]):
        j = i
        while backward_sorted_table[j + 1][2].startswith(converted):
            j += 1
        if j != i:
            str_remapping[str_index] = RemappedTranslationItem(
                str_index=backward_sorted_table[j][0],
                str_start_offset=len(backward_sorted_table[j][2]) - len(converted),
            )

    # ----- Write the string table:
    str_offsets = [-1] * len(str_table)
    offset = 0
    write_null = False
    translation_strings_text = "const char TranslationStringsData[] = {\n"
    for i, source_str in enumerate(str_table):
        if str_remapping[i] is not None:
            continue
        if write_null:
            translation_strings_text += ' "\\0"\n'
        write_null = True
        # Find what items use this string
        str_used_by = [i] + [
            j for j, r in enumerate(str_remapping) if r and r.str_index == i
        ]
        for j in str_used_by:
            for group, pre_info in [
                (str_group_messages, "messages"),
                (str_group_messageswarn, "messagesWarn"),
                (str_group_characters, "characters"),
                (str_group_settingdesc, "SettingsDescriptions"),
                (str_group_settingshortnames, "SettingsShortNames"),
                (str_group_settingmenuentries, "SettingsMenuEntries"),
                (str_group_settingmenuentriesdesc, "SettingsMenuEntriesDescriptions"),
            ]:
                for item in group:
                    if item.str_index == j:
                        translation_strings_text += (
                            f"  //     - {pre_info} {item.info}\n"
                        )
            if j == i:
                translation_strings_text += f"  // {offset: >4}: {escape(source_str)}\n"
                str_offsets[j] = offset
            else:
                remapped = str_remapping[j]
                assert remapped is not None
                translation_strings_text += f"  // {offset + remapped.str_start_offset: >4}: {escape(str_table[j])}\n"
                str_offsets[j] = offset + remapped.str_start_offset
        converted_bytes = convert_string_bytes(symbol_conversion_table, source_str)
        translation_strings_text += f'  "{bytes_to_escaped(converted_bytes)}"'
        str_offsets[i] = offset
        # Add the length and the null terminator
        offset += len(converted_bytes) + 1
    translation_strings_text += "\n}; // TranslationStringsData\n\n"

    def get_offset(idx: int) -> int:
        assert str_offsets[idx] >= 0
        return str_offsets[idx]

    translation_indices_text = "const TranslationIndexTable TranslationIndices = {\n"

    # ----- Write the messages string indices:
    for group in [str_group_messages, str_group_messageswarn, str_group_characters]:
        for item in group:
            translation_indices_text += f"  .{item.info} = {get_offset(item.str_index)}, // {escape(str_table[item.str_index])}\n"
        translation_indices_text += "\n"

    # ----- Write the settings index tables:
    for group, name in [
        (str_group_settingdesc, "SettingsDescriptions"),
        (str_group_settingshortnames, "SettingsShortNames"),
        (str_group_settingmenuentries, "SettingsMenuEntries"),
        (str_group_settingmenuentriesdesc, "SettingsMenuEntriesDescriptions"),
    ]:
        max_len = 30
        translation_indices_text += f"  .{name} = {{\n"
        for item in group:
            translation_indices_text += f"    /* {item.info.ljust(max_len)[:max_len]} */ {get_offset(item.str_index)}, // {escape(str_table[item.str_index])}\n"
        translation_indices_text += f"  }}, // {name}\n\n"

    translation_indices_text += "}; // TranslationIndices\n\n"

    return translation_strings_text + translation_indices_text


def get_translation_sanity_checks_text(defs: dict) -> str:
    sanity_checks_text = "\n// Verify SettingsItemIndex values:\n"
    for i, mod in enumerate(defs["menuOptions"]):
        eid = mod["id"]
        sanity_checks_text += (
            f"static_assert(static_cast<uint8_t>(SettingsItemIndex::{eid}) == {i});\n"
        )
    sanity_checks_text += f"static_assert(static_cast<uint8_t>(SettingsItemIndex::NUM_ITEMS) == {len(defs['menuOptions'])});\n"
    return sanity_checks_text


def read_version() -> str:
    with open(HERE.parent / "source" / "version.h") as version_file:
        for line in version_file:
            if re.findall(r"^.*(?<=(#define)).*(?<=(BUILD_VERSION))", line):
                matches = re.findall(r"\"(.+?)\"", line)
                if matches:
                    version = matches[0]
                    try:
                        version += f".{subprocess.check_output(['git', 'rev-parse', '--short=7', 'HEAD']).strip().decode('ascii').upper()}"
                    # --short=7: the shorted hash with 7 digits. Increase/decrease if needed!
                    except OSError:
                        version += " git"
    return version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-pickled",
        help="Write pickled language data for later reuse",
        type=argparse.FileType("wb"),
        required=False,
        dest="output_pickled",
    )
    parser.add_argument(
        "--input-pickled",
        help="Use previously generated pickled language data",
        type=argparse.FileType("rb"),
        required=False,
        dest="input_pickled",
    )
    parser.add_argument(
        "--strings-bin",
        help="Use generated TranslationIndices + TranslationStrings data and compress them",
        type=argparse.FileType("rb"),
        required=False,
        dest="strings_bin",
    )
    parser.add_argument(
        "--compress-font",
        help="Compress the font table",
        action="store_true",
        required=False,
        dest="compress_font",
    )
    parser.add_argument(
        "--output", "-o", help="Target file", type=argparse.FileType("w"), required=True
    )
    parser.add_argument("languageCode", help="Language to generate")
    return parser.parse_args()


def main() -> None:
    json_dir = HERE

    args = parse_args()
    if args.input_pickled and args.output_pickled:
        logging.error("error: Both --output-pickled and --input-pickled are specified")
        sys.exit(1)

    language_data: LanguageData
    if args.input_pickled:
        logging.info(f"Reading pickled language data from {args.input_pickled.name}...")
        language_data = pickle.load(args.input_pickled)
        if language_data.lang["languageCode"] != args.languageCode:
            logging.error(
                f"error: languageCode {args.languageCode} does not match language data {language_data.lang['languageCode']}"
            )
            sys.exit(1)
        logging.info(f"Read language data for {language_data.lang['languageCode']}")
        logging.info(f"Build version: {language_data.build_version}")
    else:
        try:
            build_version = read_version()
        except FileNotFoundError:
            logging.error("error: Could not find version info ")
            sys.exit(1)

        logging.info(f"Build version: {build_version}")
        logging.info(f"Making {args.languageCode} from {json_dir}")

        lang_ = read_translation(json_dir, args.languageCode)
        defs_ = load_json(os.path.join(json_dir, "translations_def.js"), True)
        language_data = prepare_language(lang_, defs_, build_version)

    out_ = args.output
    write_start(out_)
    if args.strings_bin:
        write_language(
            language_data,
            out_,
            args.strings_bin.read(),
            compress_font=args.compress_font,
        )
    else:
        write_language(language_data, out_, compress_font=args.compress_font)

    if args.output_pickled:
        logging.info(f"Writing pickled data to {args.output_pickled.name}")
        pickle.dump(language_data, args.output_pickled)

    logging.info("Done")


if __name__ == "__main__":
    main()
