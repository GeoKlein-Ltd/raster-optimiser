@echo off
rem Launches QGIS 3.44 LTR with this repo visible as a plugin source,
rem without touching any persistent user/system environment variable.
rem QGIS_PLUGINPATH is set for this process only.
rem
rem Points at .qgis_dev, not the repo root: QGIS scans every subdirectory
rem of QGIS_PLUGINPATH for a metadata.txt, so pointing at the repo root
rem makes every subdirectory in the repo root show up as "Invalid plugins" in
rem Plugin Manager. .qgis_dev contains only an NTFS junction named
rem raster_optimiser pointing at the actual package folder.
rem
rem That junction is NOT tracked by git (folder is gitignored). If you
rem clone this repo fresh, recreate it before using this launcher:
rem   mklink /J "<repo>\.qgis_dev\raster_optimiser" "<repo>\raster_optimiser"
set QGIS_PLUGINPATH=D:\GeoKlein\QGIS_Qfield\QGIS_GeoKlein_Plugins\raster_optimiser\.qgis_dev
call "C:\OSGeo4W\bin\qgis-ltr.bat" %*
