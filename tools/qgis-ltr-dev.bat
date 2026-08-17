@echo off
rem Launches QGIS 3.44 LTR with this repo visible as a plugin source,
rem without touching any persistent user/system environment variable.
rem QGIS_PLUGINPATH is set for this process only.
set QGIS_PLUGINPATH=D:\GeoKlein\QGIS_Qfield\QGIS_GeoKlein_Plugins\raster_optimiser
call "C:\OSGeo4W\bin\qgis-ltr.bat" %*
