import random
import os
import unittest
from __main__ import vtk, qt, ctk, slicer
from slicer.ScriptedLoadableModule import *
import logging
from slicer.i18n import translate
from slicer.i18n import tr as _
#
# OpenLIFUTextureModel
#

class OpenLIFUTextureModel(ScriptedLoadableModule):
  """Uses ScriptedLoadableModule base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def __init__(self, parent):
    ScriptedLoadableModule.__init__(self, parent)
    self.parent.title = _("OpenLIFU Texture Model")
    self.parent.categories = [translate("qSlicerAbstractCoreModule","OpenLIFU.OpenLIFU Modules")]
    self.parent.dependencies = []
    self.parent.contributors = ["Andras Lasso (PerkLab, Queen's)", "Amani Ibrahim (PerkLab, Queen's)" ]
    self.parent.helpText = _("""This module applies a texture (stored in a volume node) to a model node.
It is typically used to display colored surfaces, provided by surface scanners, exported in OBJ format.
The model must contain texture coordinates. Only a single texture file per model is supported.
For more information, visit <a href='https://github.com/SlicerIGT/SlicerIGT/#user-documentation'>SlicerIGT project website</a>.
""")
    self.parent.acknowledgementText = """ """ # replace with organization, grant and thanks.

#
# OpenLIFUTextureModelWidget
#

class OpenLIFUTextureModelWidget(ScriptedLoadableModuleWidget):
  """Uses ScriptedLoadableModuleWidget base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def setup(self):
    ScriptedLoadableModuleWidget.setup(self)

    # Instantiate and connect widgets ...

    #
    # Parameters Area
    #
    parametersCollapsibleButton = ctk.ctkCollapsibleButton()
    parametersCollapsibleButton.text = _("Parameters")
    self.layout.addWidget(parametersCollapsibleButton)

    # Layout within the dummy collapsible button
    parametersFormLayout = qt.QFormLayout(parametersCollapsibleButton)

    #
    # input volume selector
    #
    self.inputModelSelector = slicer.qMRMLNodeComboBox()
    self.inputModelSelector.nodeTypes = [ "vtkMRMLModelNode" ]
    self.inputModelSelector.addEnabled = False
    self.inputModelSelector.removeEnabled = True
    self.inputModelSelector.renameEnabled = True
    self.inputModelSelector.noneEnabled = False
    self.inputModelSelector.showHidden = False
    self.inputModelSelector.showChildNodeTypes = False
    self.inputModelSelector.setMRMLScene( slicer.mrmlScene )
    self.inputModelSelector.setToolTip(_( "Model node containing geometry and texture coordinates." ))
    parametersFormLayout.addRow(_("Model:"), self.inputModelSelector)

    #input texture selector
    self.inputTextureSelector = slicer.qMRMLNodeComboBox()
    self.inputTextureSelector.nodeTypes = [ "vtkMRMLVectorVolumeNode" ]
    self.inputTextureSelector.addEnabled = False
    self.inputTextureSelector.removeEnabled = True
    self.inputTextureSelector.renameEnabled = True
    self.inputTextureSelector.noneEnabled = False
    self.inputTextureSelector.showHidden = False
    self.inputTextureSelector.showChildNodeTypes = False
    self.inputTextureSelector.setMRMLScene( slicer.mrmlScene )
    self.inputTextureSelector.setToolTip(_( "Color image containing texture image." ))
    parametersFormLayout.addRow(_("Texture:"), self.inputTextureSelector)    

    #
    # Apply Button
    #
    self.applyButton = qt.QPushButton(_("Apply"))
    self.applyButton.toolTip = _("Apply texture to selected model.")
    self.applyButton.enabled = False
    parametersFormLayout.addRow(self.applyButton)

    # connections
    self.applyButton.connect('clicked(bool)', self.onApplyButton)
    self.inputModelSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.onSelect)
    self.inputTextureSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.onSelect)

    # Add vertical spacer
    self.layout.addStretch(1)

    # Refresh Apply button state
    self.onSelect()

  def cleanup(self):
    pass

  def onSelect(self):
    self.applyButton.enabled = self.inputTextureSelector.currentNode() and self.inputModelSelector.currentNode()

  def onApplyButton(self):
    try:
      qt.QApplication.setOverrideCursor(qt.Qt.WaitCursor)
      logic = OpenLIFUTextureModelLogic()
      logic.applyTexture(self.inputModelSelector.currentNode(), self.inputTextureSelector.currentNode())
      qt.QApplication.restoreOverrideCursor()
    except Exception as e:
      qt.QApplication.restoreOverrideCursor()
      slicer.util.errorDisplay("Failed to compute results: "+str(e))
      import traceback
      traceback.print_exc() 

#
# OpenLIFUTextureModelLogic
#
class OpenLIFUTextureModelLogic(ScriptedLoadableModuleLogic):
  """This class implements all the actual computations.
  Uses ScriptedLoadableModuleLogic base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def applyTexture(self, modelNode, textureImageNode, saveAsPointData=None):
    """
    Apply texture to model node
    :param saveAsPointData: None (not saved), `vector`, `float-vector`, `float-components`
    """
    self.showTextureOnModel(modelNode, textureImageNode)


  # Show texture
  def showTextureOnModel(self, modelNode, textureImageNode):
    # Shift/Scale texture map to uchar
    filter = vtk.vtkImageShiftScale()
    typeString = textureImageNode.GetImageData().GetScalarTypeAsString()
    # default
    scale = 1
    if typeString =='unsigned short':
        scale = 1 / 255.0
    filter.SetScale(scale)
    filter.SetOutputScalarTypeToUnsignedChar()
    filter.SetInputData(textureImageNode.GetImageData())
    filter.SetClampOverflow(True)
    filter.Update()

    modelDisplayNode = modelNode.GetDisplayNode()
    modelDisplayNode.SetBackfaceCulling(0)
    textureImageFlipVert = vtk.vtkImageFlip()
    textureImageFlipVert.SetFilteredAxis(1)
    textureImageFlipVert.SetInputConnection(filter.GetOutputPort())
    modelDisplayNode.SetTextureImageDataConnection(textureImageFlipVert.GetOutputPort())
 
class OpenLIFUTextureModelTest(ScriptedLoadableModuleTest):
  """
  This is the test case for your scripted module.
  Uses ScriptedLoadableModuleTest base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def setUp(self):
    """ Do whatever is needed to reset the state - typically a scene clear will be enough.
    """
    slicer.mrmlScene.Clear(0)

  def runTest(self):
    """Run as few or as many tests as needed here.
    """
    self.setUp()
    self.test_OpenLIFUTextureModel1()

  def test_OpenLIFUTextureModel1(self):
    """ Ideally you should have several levels of tests.  At the lowest level
    tests should exercise the functionality of the logic with different inputs
    (both valid and invalid).  At higher levels your tests should emulate the
    way the user would interact with your code and confirm that it still works
    the way you intended.
    One of the most important features of the tests is that it should alert other
    developers when their changes will have an impact on the behavior of your
    module.  For example, if a developer removes a feature that you depend on,
    your test should break so they know that the feature is needed.
    """

    slicer.util.delayDisplay("Starting the test")

    # Download
    import urllib
    url = 'https://github.com/Slicer/SlicerTestingData/releases/download/SHA256/752ce9afe8b708fcd4f8448612170f8e730670d845f65177860edc0e08004ecf'
    zipFilePath = slicer.app.temporaryPath + '/' + 'FemurHeadSurfaceScan.zip'
    extractPath = slicer.app.temporaryPath + '/' + 'FemurHeadSurfaceScan'
    if not os.path.exists(zipFilePath) or os.stat(zipFilePath).st_size == 0:
      logging.info('Requesting download from %s...\n' % url)
      urllib.request.urlretrieve(url, zipFilePath)
      slicer.util.delayDisplay('Finished with download\n')

    # Unzip
    slicer.util.delayDisplay("Unzipping to %s" % (extractPath))
    qt.QDir().mkpath(extractPath)
    applicationLogic = slicer.app.applicationLogic()
    applicationLogic.Unzip(zipFilePath, extractPath)

    # Load
    slicer.util.loadModel(extractPath+"/head_obj.obj")
    slicer.util.loadVolume(extractPath+"/head_obj_0.png")

    slicer.util.delayDisplay('Finished with download and loading')

    # Test
    modelNode = slicer.util.getNode("head_obj")
    textureNode = slicer.util.getNode("head_obj_0")
    logic = OpenLIFUTextureModelLogic()
    logic.applyTexture(modelNode, textureNode)
    slicer.util.delayDisplay('Test passed!')
