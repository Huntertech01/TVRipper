# TVRipper
Detects DVD boxset titles and automatically rips, separates, encodes, and renames whole DVDs, automating the process of ripping boxsets. If there is an episode list locally, the program will attempt to use it for correct file naming, then compare it to TMDB if provided with an api key. If the list shows more or fewer episodes per disc, the program will halt to prevent mis-naming.

Optional arguments to run with the program
--show    #Adds Show Name
--season    #Adds Season Number
--gorup-size    #Auto chapter grouping for stitching files into full episodes (default 3)  (Must know or guess this number. I recommend using HandBrakes GUI to view the chapter lengths before running)
--junk-threshold    #Ignores any chapters shorter than this many seconds (default:15)
--episode-file    #Optional different path to local episode list file
--tmdb-api-key    #TMDB api key. If omitted, uses TMDB_API_KEY env variable (Will need to get an api key from TMDB)
