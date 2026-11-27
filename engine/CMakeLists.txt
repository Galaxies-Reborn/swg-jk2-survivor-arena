cmake_minimum_required(VERSION 3.15)
project(jk2_arena LANGUAGES C)
add_library(jk2_arena SHARED jk2.c)
set_target_properties(jk2_arena PROPERTIES WINDOWS_EXPORT_ALL_SYMBOLS ON C_STANDARD 99)
