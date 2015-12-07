import crcmod.predefined
import RPi.GPIO as GPIO
import spidev
import struct
import time
from stpm3x import STPM3X

class stpm3x(object):

    _spiHandle = 0;
    
    def __init__(self, spiHandle):
        self._spiHandle = spiHandle
    

    def test(self):
        print 'hello world'

    def _bytes2int32_rev(self,data_bytes):
        result = 0
        result += data_bytes[0]
        result += data_bytes[1] << 8
        result += data_bytes[2] << 16
        result += data_bytes[3] << 24             
        return result

    def _bytes2int32(self,data_bytes):
        result = 0
        result += data_bytes[3]
        result += data_bytes[2] << 8
        result += data_bytes[1] << 16
        result += data_bytes[0] << 24             
        return result

    def _crc8_calc(self,data):
        crc8_func = crcmod.predefined.mkCrcFun('crc-8')
        hex_bytestring = struct.pack('>I',data)
        crc = crc8_func(hex_bytestring)
        return crc


    def _readRegister(self, addr):
        self._spiHandle.xfer2([addr, 0xFF, 0xFF, 0xFF, 0xFF])
        readbytes = self._spiHandle.readbytes(5)
        val = self._bytes2int32_rev(readbytes[0:4])
        return val

    def _writeRegister(self, address, data):
        upperMSB = (data >> 24) & 0xFF
        #print '0x{:02x}'.format(upperMSB)
        upperLSB = (data >> 16) & 0xFF
        #print '0x{:02x}'.format(upperLSB)
        lowerMSB = (data >> 8) & 0xFF
        #print '0x{:02x}'.format(lowerMSB)
        lowerLSB = data & 0xFF
        #print '0x{:02x}'.format(lowerLSB)
        
        #Generate packet for lower portion of register
        packet = self._bytes2int32([0x00, address, lowerLSB, lowerMSB])
        crc = self._crc8_calc(packet)
        self._spiHandle.xfer2([0x00, address, lowerLSB, lowerMSB, crc])

        #Generate packet for uper portion of register
        packet = self._bytes2int32([0x00, address+1, upperLSB, upperMSB])
        #print '0x{:08x}'.format(packet)
        crc = self._crc8_calc(packet)
        #print '0x{:02x}'.format(crc) 
        self._spiHandle.xfer2([0x00, address+1, upperLSB, upperMSB, crc])

        #Read back register
        readbytes = self._spiHandle.readbytes(5)
        val = self._bytes2int32_rev(readbytes[0:4])
        crc = readbytes[4]
        return {'val':val, 'crc':crc}

    def printRegister(self, value):
        print '0x{:08x}'.format(value)

    def readConfigRegs(self):
        #read configuration registers
        print 'Configuration Registers'
        for row in xrange(0,21,1):
            addr = row*2
            regvalue = self.readReg(addr)       
            print '{:02d} 0x{:02x} 0x{:08x}'.format(row, addr, regvalue)
        #end for

    def softwareReset(self):
        rd_addr = 0x04
        wr_addr = 0x05
        data = 0xFFFF
        regvalue = self.writeReg(rd_addr, wr_addr, data)


    def _modify(self, register, value):
        mask = register['mask']
        nMask = ~mask
        position = register['position']
        shift = value << position
        
        #read current value
        currentValue = self._readRegister(register['address'])
        #self.printRegister(currentValue)

        #modify value
        newValue = (currentValue & nMask) | ((value << position) & mask)

        return newValue
  
    """
    Convert function based on code found here:
    stackoverflow.com/questions/3222088/simulating-cs-sbyte-8-bit-signed-integer-casting-in-python
    """
    def convert(self, value, bits):
        x = (2 ** bits) - 1
        y = (2 ** bits) / 2
        return ((x & value ^ y) - y)

    def read(self, register):

        regValue = self._readRegister(register['address'])
        #print("Register Value: " + hex(regValue))
                                      
        #get value from register, mask and shift
        maskedValue = (regValue & register['mask']) >> register['position']
        #print("Masked Value:   " + hex(maskedValue))

        #convert signed value of various bit width to signed int
        value = self.convert(maskedValue, register['width'])
        #print ("Converted Value: " + str(value))
        
        return value

    def write(self, register, value):
        #read and modify register contents
        newValue = self._modify(register, value)
        #self.printRegister(newValue)

        #write to device
        self._writeRegister(register['address'], newValue)

        #read value from device and check if write was successful
        currentValue = self._readRegister(register['address'])
        #self.printRegister(currentValue)

        if (currentValue == newValue):
            return 0
        else:
            return -1

        

        
        

    
