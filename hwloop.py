#!./cme_hw_venv/bin/python

import RPi.GPIO as GPIO
import spidev
import time
import config
from drivers  import stpm3x    # TODO: rename
from drivers import avalanche
from drivers import Sensor, Channel
from stpm3x import STPM3X
import memcache

#create shared memory object
sharedmem = memcache.Client(['127.0.0.1:11211'], debug=0)

#Initialize SPI Devices
#setup SPI device 0
spi0dev0 = spidev.SpiDev()
spi0dev0.open(0, 0)   # TODO: read from config file
spi0dev0.mode = 3     # (CPOL = 1 | CPHA = 1) (0b11)

#setup SPI device 1
spi0dev1 = spidev.SpiDev()
spi0dev1.open(0, 1)   # TODO: read from config file
spi0dev1.mode = 3     # (CPOL = 1 | CPHA = 1) (0b11)

#setup GPIO
avalanche = avalanche()

#setup relay GPIO
print("Initialize Relays")
avalanche.relayControl(1, True)
avalanche.relayControl(2, True)
avalanche.relayControl(3, True)
avalanche.relayControl(4, True)

print("Sensor boards: Off")
print("SPI bus 0: Disabled")

print("Please wait...")
time.sleep(10);             # give capacitors on sensors boards time to discharge

print("Sensor boards: On")
avalanche.sensorPower(True)
time.sleep(1);

print("SPI bus 0: Enabled")
avalanche.spiBus0isolate(False)


# setup and configure sensor boards (== 'channels')
channels = [ stpm3x(spi0dev0, config.system['sensors'][0]),
			 stpm3x(spi0dev1, config.system['sensors'][1]) ]


# Setup status data transfer object (array of channels).
# This initializes as an empty list, but will get filled
# in the hw loop below.
status = { 'channels': [] }
sharedmem.set('status', status)

print("\nLoop starting...")
while(1):

	# synchronize sensors - get timestamp for data points
	timestamp = avalanche.syncSensors()

	# process the channels (sensor boards)
	for i, channel in enumerate(channels):

		# read each channel's sensors into current values
		v = channel.read(STPM3X.V2RMS) * 0.035430 # Vrms
		c = channel.gatedRead(STPM3X.C2RMS, 7) * 0.003333 # Arms

		# TODO: update the channel log data

		# update cme channel status
		if i <= (len(status['channels']) - 1):
			ch = status['channels'][i]

		else:
			s0 = Sensor(0, 'AC_VOLTAGE', 'Vrms', [ timestamp, v ])
			s1 = Sensor(1, 'AC_CURRENT', 'Arms', [ timestamp, c ])
			ch = Channel(i, [ s0, s1 ] )
			status['channels'].append(ch)

		ch.updateSensors([ [ timestamp, v], [ timestamp, c] ])

	# update shared memory object
	sharedmem.set('status', status)

	print 'status:\n%r\n\n' % sharedmem.get('status')



	for i, ch in enumerate(status['channels']):
		sStr = ''
		for j, s in enumerate(ch['sensors']):
			sStr = sStr + 'S[%d]: %f, ' % (j, s['data'][0][1]) 

		print '%f - Ch[%d] [ %s ]' % (timestamp, i, sStr) 

	print '--'

	time.sleep(1)

