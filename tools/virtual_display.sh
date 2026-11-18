#!/bin/bash
#
# Skript zum Erstellen eines virutellen Displays
#
# Version: $Id$
#


printUsage()
{
	echo "Aufruf: virtual_display.sh <option> <ID> <Display> <Displayoptionen>"
        echo "        Erstellt ein virtuelles Display"
	echo "        option:               start/stop/restart"
	echo "        ID:                   Identifikation (VNCs laufen unter ..._<ID>"
	echo "        Display               Name des Displays (z.B. localhost:10)          (muss bei stop nicht angegeben werden)"
	echo "        Displayoptionen       Groesse und Farbtiefe des Displays (z.B. 1910x1024x24)"
}

# Beende Programm
exitProgram()
{
	echo "Programm wird beendet"
	exit 1
}

# Liefert Displaynummer
# Parameter Displayname
getNumber()
{
	number=${1#*:}
	number=${number%%.*}
	number=$(printf '%02d' "$number")

	if [[ "$1" != *:* ]] || [ -z $number ]
	then
		echo "Displayname $1 fehlerhaft"
		exitProgram
	fi
}

# Backup von vorhandenem Logfile 
# Parameter Logfilename
logBackup()
{
	if [ -f $1 ]
	then
		backup=$1.bck
		mv $1 $backup
	fi
}

# Erstellt viruellen Framebuffer 
# Parameter Displaynummer
#           Displaygroesse
setXvfB()
{
	$vncBuffer "$1" $accessControl $listenTcp -screen 0 "$2" 2>/dev/null &
}

# Erstellt des VNC-Server
# Parameter Displayname
#           Displayport
setx11()
{
	logFile="$logDir/vncServer_$2.log"
	logBackup "$logFile"
	while [ -e $runFile -a -x $vncServer ]
	do
		echo "Starte VNC-Server fuer Display "$1""
		$vncServer $vncServerOptions -o $logFile -display "$1" -rfbport "$2" 2>/dev/null
		sleep 2
	done &
	sleep 1
}

# Kontrolliert ob der Port schon belegt ist 
# Parameter Portnummer
isPortUsed()
{
	output=`ss -l | grep ":$1 "`
	if [ -n "$output" ]
	then
		echo "Port $1 schon belegt!"
		echo $output
		exitProgram
	fi
}


# Festlegungen

if [ $# -ne 2 ] && [ $# -lt 4 ]
then
	echo
	printUsage
	echo 
	exit 1
fi

option=$1

if [ $# -eq 2 ] && [ "$option" != "stop" ]
then
	echo 
	printUsage
	echo 
	exit 1
else
	ident=$2
fi

if [ $# -gt 3 ]
then
	# Monitor
	displayName=$3
	getNumber $displayName
	displayNumber=":$number"
	displayPort=`expr 5900 + $number`
	displayDimension=$4
fi

# Defaultwerte setzen
passwordOption=""
vncOptions="-sb 0 -repeat -shared -safer -forever -nopw -nolookup -nocursor -noxfixes -nowf -noscr -skip_lockkeys -skip_keycodes 64,204 -wait 2 -defer 4"
accessControl=""
listenTcp="-listen tcp"

if [ $# -gt 4 ]
then
    # Alle uebergebenen Parameter durchgehen
    while [ $# -ne 0 ]
    do
        case "$1" in
            -passwdfile)  shift; passwordOption="-passwdfile $1";;
            -vncoptions)  shift; vncOptions=$1;;
            -ac)          accessControl=$1;;
        esac
        shift       # weiterschieben ...
    done

fi

vncServerOptions="-noshm $vncOptions $passwordOption"

prgDir="/var/tmp/virtualDisplay"
logDir="/var/tmp/virtualDisplay/log"

vncBuffer=$prgDir/vncBuffer_$ident
vncServer=$prgDir/vncServer_$ident

runFile=$prgDir/virtual_display_"$ident"_sh.run

#echo $displayName
#echo $displayNumber
#echo $displayPort
#echo $displayDimension
#echo $ident
#echo $vncBuffer
#echo $vncServer
#echo $vncServerOptions
#echo $accessControl



case $option in
        start)

		echo "Starte Display $displayName"
		echo "Identifikation $ident"

		mkdir -m 777 -p $prgDir
                mkdir -m 777 -p $logDir

		if [ ! -f /usr/bin/x11vnc ]
		then
			echo "/usr/bin/x11vnc nicht vorhanden"
			exitProgram
		else
			cp /usr/bin/x11vnc $vncServer
		fi
		if [ ! -f /usr/bin/Xvfb ]
		then
			if [ ! -f /usr/X11R6/bin/Xvfb ]
			then
				echo "Xvfb weder in /usr/bin/ noch in /usr/X11R6/bin/ vorhanden"
				exitProgram
			else
				cp /usr/X11R6/bin/Xvfb $vncBuffer
                listenTcp=""
			fi
		else
			cp /usr/bin/Xvfb $vncBuffer
		fi


		isPortUsed "$displayPort"

		#main()

		touch $runFile
		
                setXvfB "$displayNumber" "$displayDimension"
		sleep 1
		setx11 "$displayName" "$displayPort"
		;;

	stop)
		echo "Loesche Display mit Identifikation $ident"
		rm -rf $runFile
		killall -q $vncBuffer
		killall -q $vncServer
		sleep 2
		killall -9 -q $vncServer 
		;;

        restart)
                $0 stop $ident
                sleep 2
                $0 start $ident $displayName $displayDimension $passwordOption -vncoptions "$vncOptions" $accessControl
                ;;

        *)
		echo 
		printUsage
		echo 
		exit 1
                ;;
esac
 
exit 0

